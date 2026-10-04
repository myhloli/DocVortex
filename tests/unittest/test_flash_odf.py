from __future__ import annotations

import importlib.util
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from _native_test_utils import analyze_native_test_document
from _odf_test_utils import _PIXEL_PNG, build_odf_package, build_odp_fixture, build_ods_fixture, build_odt_fixture
from _span_test_utils import inline, inline_text, visible_content
from lxml import etree

import docvortex.analyzers.native.office.odf.table as odf_table_module
import docvortex.analyzers.native.office.odf.text as odf_text_module
from docvortex.analyzers.native import OdpModel, OdsModel, OdtModel
from docvortex.analyzers.native.office.odf.errors import OdfEncryptedError, OdfParseError, OdfResourceLimitError
from docvortex.analyzers.native.office.odf.metadata import extract_odf_metadata
from docvortex.analyzers.native.office.odf.package import OdfPackage
from docvortex.api import parse
from docvortex.document.detection import guess_suffix_by_bytes, guess_suffix_by_path
from docvortex.render import render_docx, render_html, render_markdown, render_structured_content
from docvortex.schema import BlockType


def test_odt_recovers_structure_and_all_renderers() -> None:
    """Validate ODT titles, rich text, lists, merged tables, running sequences, footnotes, formulas, and images."""
    middle, model = analyze_native_test_document(build_odt_fixture(), file_suffix="odt")
    raw_blocks = [block for page in model.pages for block in page]
    raw_types = [block["type"] for block in raw_blocks]
    assert len(model.pages) == 1
    assert BlockType.DOC_TITLE in raw_types
    assert BlockType.PARAGRAPH_TITLE in raw_types
    assert BlockType.LIST in raw_types
    assert BlockType.TABLE in raw_types
    assert BlockType.EQUATION in raw_types
    assert BlockType.IMAGE in raw_types
    assert BlockType.PAGE_FOOTNOTE in raw_types
    assert BlockType.HEADER in raw_types
    assert BlockType.FOOTER in raw_types
    assert any(
        any(isinstance(span, dict) and "bold" in span.get("styles", []) for span in block.get("content", []))
        for block in raw_blocks
        if isinstance(block.get("content"), list)
    )
    assert any(
        "rowspan" not in str(block.get("content")) and 'colspan="2"' in str(block.get("content")) for block in raw_blocks
    )
    assert any(block.get("content") == r"\frac{x}{2}" for block in raw_blocks)
    assert any(
        block.get("type") == BlockType.PAGE_FOOTNOTE and "Note body" in visible_content(block.get("content"))
        for block in raw_blocks
    )

    markdown = render_markdown(middle)
    html_output = render_html(middle)
    structured = render_structured_content(middle)
    assert "ODT Title" in markdown
    assert "<script>alert(1)</script>" not in markdown
    assert "<script>alert(1)</script>" not in html_output
    assert "<table" in html_output
    assert structured["pages"][0]["blocks"][0]["type"] == "doc_title"


def test_odt_promotes_numbered_heading_inside_list() -> None:
    """Verify that LibreOffice encoded in list-item reverts to numbered chapter headings for text:h."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0">
 <office:automatic-styles><text:list-style style:name="L1">
  <text:list-level-style-number text:level="1" style:num-format="1"/>
 </text:list-style></office:automatic-styles>
 <office:body><office:text><text:list text:style-name="L1"><text:list-item>
  <text:h text:outline-level="1">Chapter</text:h>
 </text:list-item></text:list></office:text></office:body>
</office:document-content>"""
    middle, _ = analyze_native_test_document(build_odf_package("odt", content), file_suffix="odt")
    assert middle.pages[0].blocks[0].type == BlockType.PARAGRAPH_TITLE
    assert inline_text(middle.pages[0].blocks[0].content) == "1 Chapter"  # type: ignore[union-attr]


def test_odt_inherits_document_title_semantics_from_parent_style() -> None:
    """Verify that custom paragraph styles inherit standard document title semantics along with parent-style-name."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:body><office:text><text:p text:style-name="CustomTitle">Inherited title</text:p></office:text></office:body>
</office:document-content>"""
    styles = """<office:document-styles
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0">
 <office:styles>
  <style:style style:name="Title" style:display-name="Title" style:family="paragraph"/>
  <style:style style:name="CustomTitle" style:family="paragraph" style:parent-style-name="Title"/>
 </office:styles>
</office:document-styles>"""

    pages = OdtModel().predict(BytesIO(build_odf_package("odt", content, styles_xml=styles)))

    assert pages == [[{"type": BlockType.DOC_TITLE, "level": 1, "content": inline("Inherited title")}]]


def test_odt_preserves_explicit_space_count() -> None:
    """Verify that explicit repeated spaces for text:s are not collapsed by normal XML whitespace rules."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:body><office:text><text:p>A<text:s text:c="4"/>B</text:p></office:text></office:body>
</office:document-content>"""

    pages = OdtModel().predict(BytesIO(build_odf_package("odt", content)))

    assert pages == [[{"type": BlockType.TEXT, "content": inline("A    B")}]]


def test_odt_bounds_overlong_explicit_space_count_before_integer_conversion() -> None:
    """Verify that overlong text:c is truncated at the single node limit before integer conversion, without relying on interpreter number protection."""
    assert odf_text_module._positive_space_count("9" * 100_000) == 10_000
    assert odf_text_module._positive_space_count("+0004") == 4
    assert odf_text_module._positive_space_count("-4") == 1


def test_odt_explicit_space_expansion_uses_document_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that multiple text:ss share document-level budgets and fail reliably before over-allocation."""
    monkeypatch.setattr(odf_text_module, "MAX_EXPANSION_TEXT_BYTES", 5)
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:body><office:text>
  <text:p>A<text:s text:c="3"/>B</text:p><text:p>C<text:s text:c="3"/>D</text:p>
 </office:text></office:body>
</office:document-content>"""

    with pytest.raises(OdfResourceLimitError, match="max_expansion_text_bytes=5"):
        OdtModel().predict(BytesIO(build_odf_package("odt", content)))


def test_odt_list_lifts_visual_blocks_outside_strict_list() -> None:
    """Verify that the list paragraph image retains its original type and is promoted to an ordered sibling block of LIST."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:xlink="http://www.w3.org/1999/xlink">
 <office:automatic-styles><text:list-style style:name="L1">
  <text:list-level-style-number text:level="1" style:num-format="1"/>
 </text:list-style></office:automatic-styles>
 <office:body><office:text><text:list text:style-name="L1"><text:list-item text:start-value="3">
  <text:p>Illustrated item<draw:frame><draw:image xlink:href="Pictures/pixel.png"/></draw:frame></text:p>
 </text:list-item><text:list-item><text:p>Next item</text:p></text:list-item></text:list></office:text></office:body>
</office:document-content>"""

    middle, _ = analyze_native_test_document(
        build_odf_package("odt", content, extra_parts={"Pictures/pixel.png": _PIXEL_PNG}), file_suffix="odt"
    )

    assert [block.type for block in middle.pages[0].blocks] == [BlockType.LIST, BlockType.IMAGE, BlockType.LIST]
    assert [inline_text(child.content) for child in middle.pages[0].blocks[0].content] == [  # type: ignore[union-attr]
        "3. Illustrated item"
    ]
    assert [inline_text(child.content) for child in middle.pages[0].blocks[2].content] == ["4. Next item"]  # type: ignore[union-attr]


def test_odt_ordered_list_normalizes_marker_format_and_item_restarts() -> None:
    """Verify that the ODF list retains only list-level start and outputs Arabic serial numbers continuously."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0">
 <office:automatic-styles><text:list-style style:name="L1">
  <text:list-level-style-number text:level="1" style:num-format="A" style:num-prefix="(" style:num-suffix=")"/>
 </text:list-style></office:automatic-styles>
 <office:body><office:text><text:list text:style-name="L1">
  <text:list-item text:start-value="2"><text:p>First</text:p></text:list-item>
  <text:list-item text:start-value="9"><text:p>Second</text:p></text:list-item>
  <text:list-item><text:p>Third</text:p></text:list-item>
 </text:list></office:text></office:body>
</office:document-content>"""
    middle, _ = analyze_native_test_document(build_odf_package("odt", content), file_suffix="odt")
    expected = ["2. First", "3. Second", "4. Third"]
    list_block = middle.pages[0].blocks[0]

    assert [inline_text(child.content) for child in list_block.content] == expected  # type: ignore[union-attr]
    assert render_markdown(middle).splitlines() == expected
    assert all(label in render_html(middle, standalone=False) for label in ("First", "Second", "Third"))
    structured = render_structured_content(middle)
    assert structured["pages"][0]["blocks"][0]["content"] == "\n".join(expected)
    with ZipFile(BytesIO(render_docx(middle))) as package:
        document_xml = package.read("word/document.xml").decode("utf-8")
    document_root = etree.fromstring(document_xml.encode("utf-8"))
    paragraph_texts = ["".join(paragraph.itertext()) for paragraph in document_root.xpath("//*[local-name()='p']")]
    assert all(label in paragraph_texts for label in expected)


def test_odt_list_item_joins_multiple_paragraphs_before_markers() -> None:
    """Verifying multiple paragraphs of a source list-item produces only one LIST text leaf and one marker."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:body><office:text><text:list>
  <text:list-item><text:p>First paragraph</text:p><text:p>Second paragraph</text:p></text:list-item>
  <text:list-item><text:p>Next item</text:p></text:list-item>
 </text:list></office:text></office:body>
</office:document-content>"""

    middle, _ = analyze_native_test_document(build_odf_package("odt", content), file_suffix="odt")
    list_block = middle.pages[0].blocks[0]

    assert list_block.type == BlockType.LIST
    assert [inline_text(child.content) for child in list_block.content] == [  # type: ignore[union-attr]
        "- First paragraph\nSecond paragraph",
        "- Next item",
    ]
    assert render_markdown(middle).count("- ") == 2


def test_odt_unmarked_list_style_renders_items_as_plain_text() -> None:
    """Verify that lists referencing empty list styles (no visible markup) are output as normal paragraphs, retaining LIST when the style is missing."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0">
 <office:automatic-styles>
  <text:list-style style:name="NoMarker"/>
 </office:automatic-styles>
 <office:body><office:text>
  <text:list text:style-name="NoMarker"><text:list-item><text:p>First</text:p></text:list-item><text:list-item><text:p>Second</text:p></text:list-item></text:list>
  <text:list text:style-name="Missing"><text:list-item><text:p>Keeps list</text:p></text:list-item></text:list>
 </office:text></office:body>
</office:document-content>"""
    middle, _ = analyze_native_test_document(build_odf_package("odt", content), file_suffix="odt")
    blocks = middle.pages[0].blocks

    assert [block.type for block in blocks] == [BlockType.TEXT, BlockType.TEXT, BlockType.LIST]
    assert [inline_text(block.content) for block in blocks[:2]] == ["First", "Second"]  # type: ignore[union-attr]
    assert [inline_text(child.content) for child in blocks[2].content] == ["- Keeps list"]  # type: ignore[union-attr,index]
    assert render_markdown(middle).splitlines() == ["First", "", "Second", "", "- Keeps list"]


def test_odt_table_cell_renders_inline_image_once() -> None:
    """Verify that the inline picture in the ODT/ODP cell will no longer be output repeatedly by image block outside the corresponding segment."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:xlink="http://www.w3.org/1999/xlink">
 <office:body><office:text><table:table><table:table-row><table:table-cell>
  <text:p>Cell<draw:frame><draw:image xlink:href="Pictures/pixel.png"/></draw:frame></text:p>
 </table:table-cell></table:table-row></table:table></office:text></office:body>
</office:document-content>"""

    pages = OdtModel().predict(BytesIO(build_odf_package("odt", content, extra_parts={"Pictures/pixel.png": _PIXEL_PNG})))

    assert pages[0][0]["type"] == BlockType.TABLE
    assert pages[0][0]["content"].count("<img") == 1


def test_odt_table_cell_unmarked_list_renders_as_plain_paragraphs() -> None:
    """Verify that empty list style cell lists are unpacked as <p>, and all-empty list rows are no longer left with <ul> skeletons."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">
 <office:automatic-styles>
  <text:list-style style:name="NoMarker"/>
  <text:list-style style:name="LB"><text:list-level-style-bullet text:level="1" text:bullet-char="•"/></text:list-style>
 </office:automatic-styles>
 <office:body><office:text><table:table>
  <table:table-row>
   <table:table-cell><text:list text:style-name="NoMarker"><text:list-item><text:p>Class1</text:p></text:list-item></text:list></table:table-cell>
   <table:table-cell><text:list text:style-name="LB"><text:list-item><text:p>Bulleted</text:p></text:list-item></text:list></table:table-cell>
  </table:table-row>
  <table:table-row>
   <table:table-cell><text:list text:style-name="NoMarker"><text:list-item><text:p><text:span/></text:p></text:list-item></text:list></table:table-cell>
   <table:table-cell><text:list text:style-name="NoMarker"><text:list-item><text:p><text:span/></text:p></text:list-item></text:list></table:table-cell>
  </table:table-row>
 </table:table></office:text></office:body>
</office:document-content>"""

    pages = OdtModel().predict(BytesIO(build_odf_package("odt", content)))
    table_html = pages[0][0]["content"]

    assert "<td><p>Class1</p></td>" in table_html
    assert "<ul><li>Bulleted</li></ul>" in table_html
    assert table_html.count("<ul") == 1
    assert "<li></li>" not in table_html
    assert table_html.count("<tr>") == 1  # All empty list rows are collapsed as a whole


def test_odt_table_cell_unmarked_list_keeps_styled_nested_list() -> None:
    """When verifying unpacking of unmarked lists, nested lists with true styles retain ul, and nested lists that inherit empty styles continue to unpack."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">
 <office:automatic-styles>
  <text:list-style style:name="NoMarker"/>
  <text:list-style style:name="LB"><text:list-level-style-bullet text:level="1" text:bullet-char="•"/></text:list-style>
 </office:automatic-styles>
 <office:body><office:text><table:table><table:table-row><table:table-cell>
  <text:list text:style-name="NoMarker"><text:list-item>
   <text:p>Top</text:p>
   <text:list text:style-name="LB"><text:list-item><text:p>Nested</text:p></text:list-item></text:list>
   <text:list><text:list-item><text:p>Inherited</text:p></text:list-item></text:list>
  </text:list-item></text:list>
 </table:table-cell></table:table-row></table:table></office:text></office:body>
</office:document-content>"""

    pages = OdtModel().predict(BytesIO(build_odf_package("odt", content)))

    assert pages[0][0]["content"] == (
        "<table><tbody><tr><td><p>Top</p><ul><li>Nested</li></ul><p>Inherited</p></td></tr></tbody></table>"
    )


def test_odp_table_cells_from_pptx_save_as_skip_bullet_wrappers() -> None:
    """Verify PPTX Table cells saved as ODP (each paragraph is wrapped into an empty style text:list) no longer output ul."""
    cell = (
        '<text:list text:style-name="a1"><text:list-item><text:p text:style-name="a2"'
        ' text:class-names="" text:cond-style-name=""><text:span text:style-name="a3"'
        ' text:class-names="">{}</text:span></text:p></text:list-item></text:list>'
    )
    content = f"""<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">
 <office:automatic-styles><text:list-style style:name="a1"/></office:automatic-styles>
 <office:body><office:presentation><draw:page draw:name="Test Table Slide"><draw:frame draw:name="Table 3"><table:table>
  <table:table-row><table:table-cell>{cell.format("Class1")}</table:table-cell><table:table-cell>{cell.format("R1")}</table:table-cell></table:table-row>
 </table:table></draw:frame></draw:page></office:presentation></office:body>
</office:document-content>"""

    pages = OdpModel().predict(BytesIO(build_odf_package("odp", content)))
    table_html = pages[0][0]["content"]

    assert pages[0][0]["type"] == BlockType.TABLE
    assert "<td><p>Class1</p></td><td><p>R1</p></td>" in table_html
    assert "<ul" not in table_html and "<li" not in table_html


def test_odt_soft_page_break_is_ignored_with_inline_visual() -> None:
    """Verify that soft-page-break does not break pages, does not wrap lines, and visual blocks within segments continue to be retained."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:xlink="http://www.w3.org/1999/xlink">
 <office:body><office:text><text:p>
  Before<draw:frame><draw:image xlink:href="Pictures/pixel.png"/></draw:frame><text:soft-page-break/>After
 </text:p></office:text></office:body>
</office:document-content>"""

    pages = OdtModel().predict(BytesIO(build_odf_package("odt", content, extra_parts={"Pictures/pixel.png": _PIXEL_PNG})))

    assert [[block["type"] for block in page] for page in pages] == [[BlockType.TEXT, BlockType.IMAGE]]
    assert inline_text(pages[0][0]["content"]) == "BeforeAfter"


def test_odt_list_ignores_soft_page_break_and_keeps_note_on_current_page() -> None:
    """Verify that the soft paging of the list does not split the page, and the continuous text and footnotes remain on the current chapter page."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:body><office:text><text:list>
  <text:list-item><text:p>Before<text:soft-page-break/>After
   <text:note><text:note-citation>1</text:note-citation><text:note-body><text:p>After note</text:p></text:note-body></text:note>
  </text:p></text:list-item>
  <text:list-item><text:p>Next</text:p></text:list-item>
 </text:list></office:text></office:body>
</office:document-content>"""

    pages = OdtModel().predict(BytesIO(build_odf_package("odt", content)))

    assert len(pages) == 1
    assert pages[0][0]["type"] == BlockType.LIST
    assert [inline_text(child["content"]) for child in pages[0][0]["content"]] == ["BeforeAfter [1]", "Next"]
    assert pages[0][1] == {"type": BlockType.PAGE_FOOTNOTE, "content": inline("[1] After note")}


def test_odt_ignores_physical_breaks_and_pages_only_on_master_change() -> None:
    """Verify that the normal paging style is invalid and only master-page chapter changes form virtual pages."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0">
 <office:automatic-styles>
  <style:style style:name="ChapterA" style:family="paragraph" style:master-page-name="MasterA"/>
  <style:style style:name="PhysicalBreak" style:family="paragraph" style:parent-style-name="ChapterA">
   <style:paragraph-properties fo:break-before="page" fo:break-after="page"/>
  </style:style>
  <style:style style:name="ChapterB" style:family="paragraph" style:master-page-name="MasterB"/>
 </office:automatic-styles>
 <office:body><office:text>
  <text:p text:style-name="ChapterA">Before<text:soft-page-break/>Soft</text:p>
  <text:section><text:p text:style-name="PhysicalBreak">Physical</text:p></text:section>
  <text:p text:style-name="ChapterB">After</text:p>
 </office:text></office:body>
</office:document-content>"""
    styles = """<office:document-styles
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:master-styles>
  <style:master-page style:name="MasterA"><style:header><text:p>Header A</text:p></style:header></style:master-page>
  <style:master-page style:name="MasterB"><style:header><text:p>Header B</text:p></style:header></style:master-page>
 </office:master-styles>
</office:document-styles>"""

    pages = OdtModel().predict(BytesIO(build_odf_package("odt", content, styles_xml=styles)))

    assert [[visible_content(block.get("content")) for block in page] for page in pages] == [
        ["BeforeSoft", "Physical", "Header A"],
        ["After", "Header B"],
    ]


def test_odt_list_master_changes_split_pages_and_preserve_numbering_notes() -> None:
    """master changes in verification list entries will cut pages and maintain numbering, footer, and header attribution."""
    content = """<office:document-content
     xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
     xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
     xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
     <office:automatic-styles>
      <style:style style:name="A" style:family="paragraph" style:master-page-name="MasterA"/>
      <style:style style:name="B" style:family="paragraph" style:master-page-name="MasterB"/>
      <text:list-style style:name="L1">
       <text:list-level-style-number text:level="1" style:num-format="1" text:start-value="3"/>
      </text:list-style>
     </office:automatic-styles>
     <office:body><office:text>
      <text:p text:style-name="A">Before list</text:p>
      <text:list text:style-name="L1">
       <text:list-item><text:p>First <text:note><text:note-citation>1</text:note-citation>
        <text:note-body><text:p>First note</text:p></text:note-body></text:note></text:p></text:list-item>
       <text:list-item><text:p text:style-name="B">Second <text:note><text:note-citation>2</text:note-citation>
        <text:note-body><text:p>Second note</text:p></text:note-body></text:note></text:p></text:list-item>
       <text:list-item><text:p>Third</text:p></text:list-item>
      </text:list>
     </office:text></office:body></office:document-content>"""
    styles = """<office:document-styles
     xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
     xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
     xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
     <office:master-styles>
      <style:master-page style:name="MasterA">
       <style:header><text:p>Header A</text:p></style:header>
      </style:master-page>
      <style:master-page style:name="MasterB">
       <style:header><text:p>Header B</text:p></style:header>
      </style:master-page>
     </office:master-styles></office:document-styles>"""

    pages = OdtModel().predict(BytesIO(build_odf_package("odt", content, styles_xml=styles)))

    assert len(pages) == 2
    first_list = next(block for block in pages[0] if block["type"] == BlockType.LIST)
    second_list = next(block for block in pages[1] if block["type"] == BlockType.LIST)
    assert first_list["start"] == 3
    assert second_list["start"] == 4
    assert [inline_text(child["content"]) for child in first_list["content"]] == ["First [1]"]
    assert [inline_text(child["content"]) for child in second_list["content"]] == ["Second [2]", "Third"]
    assert any(block["type"] == BlockType.PAGE_FOOTNOTE and "First note" in inline_text(block["content"]) for block in pages[0])
    assert any(
        block["type"] == BlockType.PAGE_FOOTNOTE and "Second note" in inline_text(block["content"]) for block in pages[1]
    )
    assert any(block["type"] == BlockType.HEADER and inline_text(block["content"]) == "Header A" for block in pages[0])
    assert any(block["type"] == BlockType.HEADER and inline_text(block["content"]) == "Header B" for block in pages[1])


def test_odt_list_can_select_nondefault_master_on_first_page() -> None:
    """Verify that the document starts with the list using the master-page requested for the first list paragraph."""
    content = """<office:document-content
     xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
     xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
     xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
     <office:automatic-styles>
      <style:style style:name="B" style:family="paragraph" style:master-page-name="MasterB"/>
     </office:automatic-styles>
     <office:body><office:text><text:list><text:list-item>
      <text:p text:style-name="B">First list item</text:p>
     </text:list-item></text:list></office:text></office:body></office:document-content>"""
    styles = """<office:document-styles
     xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
     xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
     xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
     <office:master-styles>
      <style:master-page style:name="MasterA">
       <style:header><text:p>Header A</text:p></style:header>
      </style:master-page>
      <style:master-page style:name="MasterB">
       <style:header><text:p>Header B</text:p></style:header>
      </style:master-page>
     </office:master-styles></office:document-styles>"""

    pages = OdtModel().predict(BytesIO(build_odf_package("odt", content, styles_xml=styles)))

    assert len(pages) == 1
    assert any(block["type"] == BlockType.HEADER and inline_text(block["content"]) == "Header B" for block in pages[0])
    assert all(visible_content(block.get("content")) != "Header A" for block in pages[0])


def test_odf_covered_placeholder_reuses_colspan_coordinate() -> None:
    """covered after verifying colspan placeholder does not additionally widen the table."""
    table = etree.fromstring(
        """<table:table xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <table:table-row>
  <table:table-cell table:number-columns-spanned="2"><text:p>Merged</text:p></table:table-cell>
  <table:covered-table-cell/><table:table-cell><text:p>Tail</text:p></table:table-cell>
 </table:table-row>
 <table:table-row>
  <table:table-cell><text:p>A</text:p></table:table-cell>
  <table:table-cell><text:p>B</text:p></table:table-cell>
  <table:table-cell><text:p>C</text:p></table:table-cell>
 </table:table-row>
</table:table>""".encode()
    )

    grid = odf_table_module.parse_table_grid(table, lambda cell: "".join(cell.itertext()).strip())

    assert grid.width == 3
    assert grid.covered == {(0, 1)}
    assert grid.rows[0][0] is not None and grid.rows[0][0].col_span == 2
    assert grid.rows[0][2] is not None and grid.rows[0][2].html == "Tail"
    assert [cell.html if cell is not None else None for cell in grid.rows[1]] == ["A", "B", "C"]


def test_odt_note_after_soft_page_break_stays_on_current_page() -> None:
    """Verify that after soft-page-break is ignored, note, reference and the text still belong to the current chapter page."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:body><office:text><text:p>Before<text:soft-page-break/>After
  <text:note><text:note-citation>1</text:note-citation><text:note-body><text:p>After note</text:p></text:note-body></text:note>
 </text:p></office:text></office:body>
</office:document-content>"""

    pages = OdtModel().predict(BytesIO(build_odf_package("odt", content)))

    assert pages == [
        [
            {"type": BlockType.TEXT, "content": inline("BeforeAfter [1]")},
            {"type": BlockType.PAGE_FOOTNOTE, "content": inline("[1] After note")},
        ]
    ]


def test_ods_cell_note_emits_page_footnote() -> None:
    """Verify that the note body corresponding to ODS cell citation is output at the end of the current sheet page."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">
 <office:body><office:spreadsheet><table:table table:name="Sheet1"><table:table-row><table:table-cell><text:p>Cell
  <text:note><text:note-citation>1</text:note-citation><text:note-body><text:p>Cell note</text:p></text:note-body></text:note>
 </text:p></table:table-cell></table:table-row></table:table></office:spreadsheet></office:body>
</office:document-content>"""

    pages = OdsModel().predict(BytesIO(build_odf_package("ods", content)))

    assert [block["type"] for block in pages[0]] == [BlockType.TEXT, BlockType.PAGE_FOOTNOTE]
    assert inline_text(pages[0][0]["content"]) == "Cell [1]"
    assert inline_text(pages[0][1]["content"]) == "[1] Cell note"


def test_odp_slide_inline_note_emits_page_footnote() -> None:
    """Verify that note body in the body of ODP slide is not missed by the presentation notes path."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:body><office:presentation><draw:page draw:name="Slide1"><draw:frame><draw:text-box><text:p>Slide
  <text:note><text:note-citation>1</text:note-citation><text:note-body><text:p>Inline note</text:p></text:note-body></text:note>
 </text:p></draw:text-box></draw:frame></draw:page></office:presentation></office:body>
</office:document-content>"""

    pages = OdpModel().predict(BytesIO(build_odf_package("odp", content)))

    assert pages == [
        [
            {"type": BlockType.TEXT, "content": inline("Slide [1]")},
            {"type": BlockType.PAGE_FOOTNOTE, "content": inline("[1] Inline note")},
        ]
    ]


def test_odp_preserves_empty_slide_chart_preview_and_notes() -> None:
    """Verify that ODP is empty and slide is not lost. The chart retains data and preview at the same time. Notes belong to the original page."""
    middle, model = analyze_native_test_document(build_odp_fixture(), file_suffix="odp")
    assert len(model.pages) == 3
    assert model.pages[1] == []
    assert model.pages[0][0]["type"] == BlockType.DOC_TITLE
    chart = next(block for block in model.pages[2] if block["type"] == BlockType.CHART)
    assert "Category" in chart["content"]
    assert "Value" in chart["content"]
    assert chart["image_base64"].startswith("data:image/")
    assert any(
        block["type"] == BlockType.PAGE_FOOTNOTE and "Speaker note" in inline_text(block["content"]) for block in model.pages[2]
    )
    assert len(middle.pages) == 3


def test_ods_skips_hidden_sheet_and_emits_tables_images_and_charts() -> None:
    """Verify that ODS is visible sheet boundaries, typed value, merged structures, and chart objects."""
    middle, model = analyze_native_test_document(build_ods_fixture(), file_suffix="ods")
    assert len(model.pages) == 2
    assert [inline_text(page[0]["content"]) for page in model.pages] == ["Visible A", "Visible B"]
    flattened = [block for page in model.pages for block in page]
    assert "secret" not in str(flattened)
    assert "50%" in str(flattened)
    assert 'colspan="2"' in str(flattened)
    assert any(block["type"] == BlockType.CHART for block in flattened)
    assert len(middle.pages) == 2


def test_ods_resolves_inherited_table_visibility_with_child_override() -> None:
    """Verify that table display is inherited along the parent style, and the explicit display of the child style can be overridden and hidden."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:automatic-styles>
  <style:style style:name="HiddenParent" style:family="table">
   <style:table-properties table:display="false"/>
  </style:style>
  <style:style style:name="InheritedHidden" style:family="table" style:parent-style-name="HiddenParent"/>
  <style:style style:name="ExplicitVisible" style:family="table" style:parent-style-name="HiddenParent">
   <style:table-properties table:display="true"/>
  </style:style>
 </office:automatic-styles>
 <office:body><office:spreadsheet>
  <table:table table:name="Secret" table:style-name="InheritedHidden">
   <table:table-row><table:table-cell><text:p>secret</text:p></table:table-cell></table:table-row>
  </table:table>
  <table:table table:name="Override" table:style-name="ExplicitVisible">
   <table:table-row><table:table-cell><text:p>override</text:p></table:table-cell></table:table-row>
  </table:table>
  <table:table table:name="DefaultVisible">
   <table:table-row><table:table-cell><text:p>default</text:p></table:table-cell></table:table-row>
  </table:table>
 </office:spreadsheet></office:body>
</office:document-content>"""
    payload = build_odf_package("ods", content)

    pages = OdsModel().predict(BytesIO(payload))
    metadata = extract_odf_metadata(BytesIO(payload), "ods")

    assert len(pages) == 2
    assert "secret" not in str(pages)
    assert "override" in str(pages)
    assert "default" in str(pages)
    assert metadata["page_count"] == 2


@pytest.mark.parametrize(
    ("suffix", "payload"),
    [("odt", build_odt_fixture()), ("ods", build_ods_fixture()), ("odp", build_odp_fixture())],
    ids=["odt", "ods", "odp"],
)
def test_odf_content_detection_precedes_csv_extension(
    tmp_path: Path,
    suffix: str,
    payload: bytes,
) -> None:
    """Verify ODF Strong Content Identity Coverage masquerades extensions and CSV Signature-less cover."""
    disguised = tmp_path / "disguised.csv"
    disguised.write_bytes(payload)
    assert guess_suffix_by_bytes(payload, str(disguised)) == suffix
    assert guess_suffix_by_path(disguised) == suffix


def test_rtf_signature_still_precedes_odf_extension(tmp_path: Path) -> None:
    """Verify that adding the ZIP probe does not change the highest priority of the RTF strong signature."""
    source = tmp_path / "disguised.odt"
    source.write_bytes(rb"{\rtf1\ansi visible}")
    assert guess_suffix_by_path(source) == "rtf"
    assert guess_suffix_by_bytes(source.read_bytes(), str(source)) == "rtf"


def test_plain_text_renamed_to_odf_is_not_accepted(tmp_path: Path) -> None:
    """Verification The ODF extension by itself cannot upgrade ordinary text to a structured document."""
    source = tmp_path / "fake.odt"
    source.write_text("a,b\n1,2\n", encoding="utf-8")
    assert guess_suffix_by_path(source) not in {"odt", "ods", "odp"}
    with pytest.raises(ValueError, match="Unsupported native input format: txt"):
        parse(source)


def test_odf_rejects_mismatched_encrypted_and_expanding_packages() -> None:
    """Validation format mismatch, manifest encryption, and oversize duplicate rows fail stably before allocation."""
    with pytest.raises(OdfParseError, match="expected"):
        OdtModel().predict(BytesIO(build_ods_fixture()))

    encrypted_content = (
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0">'
        "<office:body><office:text/></office:body></office:document-content>"
    )
    encrypted = build_odf_package("odt", encrypted_content, encrypted=True)
    with pytest.raises(OdfEncryptedError, match="Encrypted ODF"):
        OdtModel().predict(BytesIO(encrypted))

    expanding_content = (
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        '<office:body><office:spreadsheet><table:table><table:table-row table:number-rows-repeated="4000001">'
        "<table:table-cell><text:p>x</text:p></table:table-cell>"
        "</table:table-row></table:table></office:spreadsheet></office:body></office:document-content>"
    )
    expanding = build_odf_package("ods", expanding_content)
    with pytest.raises(OdfResourceLimitError, match="max_grid_slots"):
        OdsModel().predict(BytesIO(expanding))


@pytest.mark.parametrize("span_attribute", ["number-rows-spanned", "number-columns-spanned"])
def test_odf_rejects_oversized_cell_spans_before_grid_materialization(
    monkeypatch: pytest.MonkeyPatch,
    span_attribute: str,
) -> None:
    """Validation of very large row and column spans fails immediately before rendering the cell or expanding the grid."""
    monkeypatch.setattr(odf_table_module, "MAX_GRID_SLOTS", 4)

    def unexpected_materialization(*_args: object, **_kwargs: object) -> None:
        """Overrun span No cell rendering or grid expansion allowed."""
        pytest.fail("oversized span reached grid materialization")

    monkeypatch.setattr(odf_table_module, "_ensure_row", unexpected_materialization)
    table = etree.fromstring(
        f'<table:table xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">'
        f'<table:table-row><table:table-cell table:{span_attribute}="5"/></table:table-row>'
        "</table:table>"
    )

    with pytest.raises(OdfResourceLimitError, match="max_grid_slots"):
        odf_table_module.parse_table_grid(table, unexpected_materialization)


def test_odf_rejects_projected_span_extent_before_extending_existing_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that a single span is legal but the cumulative width exceeds the limit without first extending the existing row."""
    monkeypatch.setattr(odf_table_module, "MAX_GRID_SLOTS", 4)
    original_ensure_row = odf_table_module._ensure_row
    observed_widths: list[int] = []

    def tracking_ensure_row(grid: object, row_index: int, width: int = 0) -> object:
        """Record the actual expansion width to ensure that the shared budget is not exceeded before failure."""
        observed_widths.append(width)
        return original_ensure_row(grid, row_index, width)  # type: ignore[arg-type]

    monkeypatch.setattr(odf_table_module, "_ensure_row", tracking_ensure_row)
    table = etree.fromstring(
        '<table:table xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">'
        '<table:table-row><table:table-cell table:number-columns-spanned="3"/>'
        '<table:table-cell table:number-columns-spanned="2"/></table:table-row>'
        "</table:table>"
    )

    with pytest.raises(OdfResourceLimitError, match="max_grid_slots"):
        odf_table_module.parse_table_grid(table, lambda _cell: "x")
    assert observed_widths and max(observed_widths) <= 3


def test_odf_rejects_overlong_repeat_before_integer_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify overlong repeat count triggers grid budget before int and cell rendering."""
    monkeypatch.setattr(odf_table_module, "MAX_GRID_SLOTS", 4)

    def unexpected_render(_cell: object) -> str:
        """Overrun repeat Cell rendering must not be entered."""
        pytest.fail("oversized ODF repeat reached cell rendering")

    table = etree.fromstring(
        (
            '<table:table xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">'
            f'<table:table-row table:number-rows-repeated="{"9" * 100_000}">'
            "<table:table-cell/>"
            "</table:table-row></table:table>"
        ).encode()
    )

    with pytest.raises(OdfResourceLimitError, match="max_grid_slots"):
        odf_table_module.parse_table_grid(table, unexpected_render)


def test_odf_skips_trailing_repeated_empty_filler_rows() -> None:
    """Verify that trailing empty filler rows for LibreOffice full grid declarations do not consume the grid budget."""
    table_xml = (
        '<table:table xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        "<table:table-row>"
        '<table:table-cell><text:p>a</text:p></table:table-cell>'
        '<table:table-cell><text:p>b</text:p></table:table-cell>'
        '<table:table-cell><text:p>c</text:p></table:table-cell>'
        '<table:table-cell><text:p>d</text:p></table:table-cell>'
        '<table:table-cell table:number-columns-repeated="16380"/>'
        "</table:table-row>"
        '<table:table-row table:number-rows-repeated="1048574">'
        '<table:table-cell table:number-columns-repeated="16384"/>'
        "</table:table-row>"
        "</table:table>"
    )

    grid = odf_table_module.parse_table_grid(etree.fromstring(table_xml.encode()), lambda cell: "".join(cell.itertext()))

    assert len(grid.rows) == 1
    assert grid.width == 4
    assert [cell.html if cell else "" for cell in grid.rows[0]] == ["a", "b", "c", "d"]


def test_odf_skips_wide_empty_filler_rows_between_content_regions() -> None:
    """Validate that full-width blank lines between content areas count against the grid budget only by their materialized width."""
    table_xml = (
        '<table:table xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        '<table:table-row><table:table-cell><text:p>a</text:p></table:table-cell></table:table-row>'
        '<table:table-row table:number-rows-repeated="300">'
        '<table:table-cell table:number-columns-repeated="16384"/></table:table-row>'
        '<table:table-row><table:table-cell><text:p>b</text:p></table:table-cell></table:table-row>'
        "</table:table>"
    )

    grid = odf_table_module.parse_table_grid(etree.fromstring(table_xml.encode()), lambda cell: "".join(cell.itertext()))

    assert len(grid.rows) == 302
    assert grid.width == 1
    assert len(odf_table_module.split_table_regions(grid)) == 2


def test_odf_column_offset_region_drops_leading_empty_rows() -> None:
    """Verify that the bounding box of a column-shifted data region does not carry an entire row with a leading blank line."""

    def cell(text: str) -> str:
        return f'<table:table-cell><text:p>{text}</text:p></table:table-cell>'

    def row(*cells_xml: str) -> str:
        return f"<table:table-row>{''.join(cells_xml)}</table:table-row>"

    gap = '<table:table-cell table:number-columns-repeated="2"/>'
    table_xml = (
        '<table:table xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        + row(*[cell("L") for _ in range(4)]) * 4
        + row(*[cell("L") for _ in range(4)], gap, cell("col-1"), cell("col-2"), cell("col-3"))
        + row(*[cell("L") for _ in range(4)], gap, cell("1"), cell("2"), cell("3")) * 4
        + "</table:table>"
    )

    grid = odf_table_module.parse_table_grid(etree.fromstring(table_xml.encode()), lambda cell: "".join(cell.itertext()))
    regions = odf_table_module.split_table_regions(grid)

    assert [(len(region.rows), region.width) for region in regions] == [(9, 4), (5, 3)]
    assert [cell.html if cell else "" for cell in regions[1].rows[0]] == ["col-1", "col-2", "col-3"]


def test_odf_single_blank_column_splits_overlapping_side_by_side_tables() -> None:
    """Verify that side-by-side tables with every other column empty and overlapping row ranges are split by connected domains."""

    def cell(text: str) -> str:
        return f'<table:table-cell><text:p>{text}</text:p></table:table-cell>'

    def row(*cells_xml: str) -> str:
        return f"<table:table-row>{''.join(cells_xml)}</table:table-row>"

    one_blank = '<table:table-cell/>'
    wide_gap = '<table:table-cell table:number-columns-repeated="4"/>'
    table_xml = (
        '<table:table xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        + row(cell("a0"), cell("a1"), cell("a2"))
        + row(cell("b0"), cell("b1"), cell("b2"))
        + row(cell("c0"), cell("c1"), cell("c2"), one_blank, cell("d0"), cell("d1"), cell("d2"))
        + row(wide_gap, cell("e0"), cell("e1"), cell("e2"))
        + row(wide_gap, cell("f0"), cell("f1"), cell("f2"))
        + "</table:table>"
    )

    grid = odf_table_module.parse_table_grid(etree.fromstring(table_xml.encode()), lambda cell: "".join(cell.itertext()))
    regions = odf_table_module.split_table_regions(grid)

    assert [(len(region.rows), region.width) for region in regions] == [(3, 3), (3, 3)]


def test_odf_single_blank_row_splits_like_excel_gap_selection() -> None:
    """Verify that single-blank-line-delimited data bands are consistent with the Excel projection and select zero-tolerance splitting."""

    def cell(text: str) -> str:
        return f'<table:table-cell><text:p>{text}</text:p></table:table-cell>'

    blank_row = '<table:table-row><table:table-cell table:number-columns-repeated="3"/></table:table-row>'
    table_xml = (
        '<table:table xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        f"<table:table-row>{cell('v00')}{cell('v01')}{cell('v02')}</table:table-row>"
        f"<table:table-row>{cell('v10')}{cell('v11')}{cell('v12')}</table:table-row>"
        f"{blank_row}"
        f"<table:table-row>{cell('v30')}{cell('v31')}{cell('v32')}</table:table-row>"
        "</table:table>"
    )

    grid = odf_table_module.parse_table_grid(etree.fromstring(table_xml.encode()), lambda cell: "".join(cell.itertext()))
    regions = odf_table_module.split_table_regions(grid)

    assert [(len(region.rows), region.width) for region in regions] == [(2, 3), (1, 3)]


def test_ods_singleton_regions_downgrade_to_text_blocks() -> None:
    """Verify that the unstructured cell area is downgraded to a text block, and the structured cells are still output as a table."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:body><office:spreadsheet><table:table table:name="Sheet1">
  <table:table-row><table:table-cell><text:p>solo</text:p></table:table-cell></table:table-row>
  <table:table-row table:number-rows-repeated="3"><table:table-cell table:number-columns-repeated="2"/></table:table-row>
  <table:table-row><table:table-cell table:number-columns-repeated="2"/><table:table-cell>
   <table:table><table:table-row><table:table-cell><text:p>inner</text:p></table:table-cell></table:table-row></table:table>
  </table:table-cell></table:table-row>
 </table:table></office:spreadsheet></office:body>
</office:document-content>"""

    pages = OdsModel().predict(BytesIO(build_odf_package("ods", content)))

    assert [block["type"] for block in pages[0]] == [BlockType.TEXT, BlockType.TABLE]
    assert inline_text(pages[0][0]["content"]) == "solo"
    assert "inner" in str(pages[0][1]["content"])


def test_ods_demo_workbook_splits_regions_like_excel_workbook() -> None:
    """Verify that the region split of the real ODS workbook is consistent with the Excel projection and has no leading blank lines."""
    demo_path = Path(__file__).resolve().parents[2] / "demo" / "open_office_docs" / "xlsx_01.ods"
    with demo_path.open("rb") as handle:
        pages = OdsModel().predict(handle)

    assert len(pages) == 3

    sheet_two_tables = [block for block in pages[1] if block["type"] == BlockType.TABLE]
    assert [str(block["content"]).count("<tr>") for block in sheet_two_tables] == [9, 5, 5]
    for block in sheet_two_tables:
        first_row = str(block["content"]).split("</tr>")[0]
        assert "<p>col-" in first_row

    sheet_three_tables = [block for block in pages[2] if block["type"] == BlockType.TABLE]
    assert [str(block["content"]).count("<tr>") for block in sheet_three_tables] == [7, 7]


def test_odf_document_grid_budget_is_shared_across_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that multiple independent tables consume the same document grid budget."""
    monkeypatch.setattr(odf_table_module, "MAX_GRID_SLOTS", 4)
    budget = odf_table_module.OdfTableExpansionBudget()
    table_xml = (
        '<table:table xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">'
        '<table:table-row table:number-rows-repeated="2">'
        '<table:table-cell table:number-columns-repeated="2"/>'
        "</table:table-row></table:table>"
    )

    first = odf_table_module.parse_table_grid(
        etree.fromstring(table_xml.encode()),
        lambda _cell: "x",
        expansion_budget=budget,
    )
    assert len(first.rows) * first.width == 4
    with pytest.raises(OdfResourceLimitError, match="max_grid_slots"):
        odf_table_module.parse_table_grid(
            etree.fromstring(table_xml.encode()),
            lambda _cell: "x",
            expansion_budget=budget,
        )


def test_odf_document_text_expansion_budget_is_shared_across_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that duplicate cell text across multiple tables collectively consumes the document-level byte budget."""
    monkeypatch.setattr(odf_table_module, "MAX_GRID_SLOTS", 100)
    monkeypatch.setattr(odf_table_module, "MAX_EXPANSION_TEXT_BYTES", 4)
    budget = odf_table_module.OdfTableExpansionBudget()
    table_xml = (
        '<table:table xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">'
        '<table:table-row><table:table-cell table:number-columns-repeated="2"/></table:table-row>'
        "</table:table>"
    )

    odf_table_module.parse_table_grid(
        etree.fromstring(table_xml.encode()),
        lambda _cell: "xxx",
        expansion_budget=budget,
    )
    with pytest.raises(OdfResourceLimitError, match="max_expansion_text_bytes"):
        odf_table_module.parse_table_grid(
            etree.fromstring(table_xml.encode()),
            lambda _cell: "xxx",
            expansion_budget=budget,
        )


def test_odt_parser_wires_one_table_budget_across_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that multiple common tables in ODT share the same document budget via parser."""
    monkeypatch.setattr(odf_table_module, "MAX_GRID_SLOTS", 4)
    table = """<table:table><table:table-row table:number-rows-repeated="2">
     <table:table-cell table:number-columns-repeated="2"><text:p>x</text:p></table:table-cell>
    </table:table-row></table:table>"""
    content = f"""<office:document-content
     xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
     xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
     xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
     <office:body><office:text>{table}{table}</office:text></office:body>
    </office:document-content>"""

    with pytest.raises(OdfResourceLimitError, match="max_grid_slots"):
        OdtModel().predict(BytesIO(build_odf_package("odt", content)))


def test_odf_rejects_overlong_chart_columns_before_bigint_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validation chart A1 column name subject to shared grid budget before big integer conversion."""
    monkeypatch.setattr(odf_table_module, "MAX_GRID_SLOTS", 4)

    assert odf_table_module.parse_cell_range_bounds("local-table.D1:D2") == (0, 1, 3, 3)
    assert odf_table_module.parse_cell_range_bounds("local-table.E1:E2") is None
    assert odf_table_module.parse_cell_range_bounds(f"local-table.{'A' * 100_000}1") is None


def test_odf_rejects_overlong_chart_rows_before_bigint_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verification chart A1 row number is subject to shared grid budget before int conversion."""
    monkeypatch.setattr(odf_table_module, "MAX_GRID_SLOTS", 4)

    assert odf_table_module.parse_cell_range_bounds("local-table.A4:B4") == (3, 3, 0, 1)
    assert odf_table_module.parse_cell_range_bounds("local-table.A5:B5") is None
    assert odf_table_module.parse_cell_range_bounds(f"local-table.A{'9' * 100_000}") is None


@pytest.mark.parametrize(
    "target",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/plain,unsafe",
        "vbscript:msgbox(1)",
        "file:///tmp/unsafe",
        "ftp://example.com/file",
        "//example.com/path",
        "\\\\server\\share",
    ],
)
def test_odf_rejects_unsafe_hyperlinks_before_shared_renderers(target: str) -> None:
    """Verification Danger ODF link degraded during Raw stage, Markdown and DOCX no longer carry targets."""
    content = f'''<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:xlink="http://www.w3.org/1999/xlink">
 <office:body><office:text><text:p>before <text:a xlink:href="{target}">click</text:a> after</text:p>
 </office:text></office:body></office:document-content>'''
    payload = build_odf_package("odt", content)
    pages = OdtModel().predict(BytesIO(payload))
    middle, _ = analyze_native_test_document(payload, file_suffix="odt")
    markdown = render_markdown(middle)
    docx = render_docx(middle)
    with ZipFile(BytesIO(docx)) as package:
        relationships = package.read("word/_rels/document.xml.rels").decode("utf-8")

    assert pages == [[{"type": BlockType.TEXT, "content": inline("before click after")}]]
    assert markdown == "before click after"
    assert target not in relationships


@pytest.mark.parametrize(
    "target",
    [
        "https://example.com/path",
        "mailto:reader@example.com",
        "tel:+123456",
        "chapter.odt#section-one",
    ],
)
def test_odf_preserves_allowed_external_and_relative_hyperlinks(target: str) -> None:
    """Verification allows the protocol, relative address, and fragment to go directly to HyperlinkSpan."""
    content = f'''<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:xlink="http://www.w3.org/1999/xlink">
 <office:body><office:text><text:p><text:a xlink:href="{target}">click</text:a></text:p>
 </office:text></office:body></office:document-content>'''
    pages = OdtModel().predict(BytesIO(build_odf_package("odt", content)))

    assert pages[0][0]["type"] == BlockType.TEXT
    assert len(pages[0][0]["content"]) == 1
    link = pages[0][0]["content"][0]
    assert link["type"] == "hyperlink"
    assert link["url"] == target
    assert inline_text(link["content"]) == "click"


def test_odf_preserves_title_fragment_and_drops_unemittable_text_fragment() -> None:
    """Verify that local fragment only links to bookmark that is actually exposed by header class block."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:xlink="http://www.w3.org/1999/xlink">
 <office:body><office:text>
  <text:p><text:a xlink:href="#title-target">Title jump</text:a> / <text:a xlink:href="#text-target">Text jump</text:a></text:p>
  <text:h text:outline-level="1"><text:bookmark-start text:name="title-target"/>Heading</text:h>
  <text:p><text:bookmark text:name="text-target"/>Ordinary target</text:p>
 </office:text></office:body>
</office:document-content>"""

    pages = OdtModel().predict(BytesIO(build_odf_package("odt", content)))

    assert inline_text(pages[0][0]["content"]) == "Title jump / Text jump"
    assert pages[0][0]["content"][0]["type"] == "hyperlink"
    assert pages[0][0]["content"][0]["url"] == "#title-target"
    assert pages[0][1]["type"] == BlockType.PARAGRAPH_TITLE
    assert pages[0][1]["anchor"] == "title-target"
    assert pages[0][2] == {"type": BlockType.TEXT, "content": inline("Ordinary target")}


def test_odf_corrupt_optional_styles_and_external_image_degrade_locally() -> None:
    """Verify that optional style corruption and external images do not block body text or trigger network reads."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:xlink="http://www.w3.org/1999/xlink">
 <office:body><office:text><text:p>visible</text:p>
  <draw:frame><draw:image xlink:href="https://example.com/external.png"/></draw:frame>
 </office:text></office:body></office:document-content>"""
    pages = OdtModel().predict(BytesIO(build_odf_package("odt", content, styles_xml="<broken")))
    assert pages == [[{"type": BlockType.TEXT, "content": inline("visible")}]]


def test_odf_malformed_image_and_object_references_degrade_locally() -> None:
    """Verify illegal pictures and objects URI only discard resources and do not block ODF text."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:xlink="http://www.w3.org/1999/xlink">
 <office:body><office:text><text:p>visible</text:p><draw:frame>
  <draw:object xlink:href="http://["/><draw:image xlink:href="http://["/>
 </draw:frame></office:text></office:body></office:document-content>"""

    pages = OdtModel().predict(BytesIO(build_odf_package("odt", content)))

    assert pages == [[{"type": BlockType.TEXT, "content": inline("visible")}]]


def test_odf_flattened_titles_and_notes_keep_tag_literals_as_text_spans() -> None:
    """Verify that titles, speaker notes, and inline footnotes retain the original appearance of labels but do not generate links Span."""
    literal = "&lt;hyperlink&gt;&lt;text&gt;click&lt;/text&gt;&lt;url&gt;javascript:alert(1)&lt;/url&gt;&lt;/hyperlink&gt;"
    odp_content = f"""<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:body><office:presentation><draw:page draw:name="Slide 1">
  <draw:frame presentation:class="title"><draw:text-box><text:p>{literal}</text:p></draw:text-box></draw:frame>
  <presentation:notes><draw:frame><draw:text-box><text:p>{literal}</text:p></draw:text-box></draw:frame></presentation:notes>
 </draw:page></office:presentation></office:body></office:document-content>"""
    odt_content = f"""<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:body><office:text><text:p>Body<text:note><text:note-citation>{literal}</text:note-citation>
  <text:note-body><text:p>Footnote body {literal}</text:p></text:note-body>
 </text:note></text:p></office:text></office:body></office:document-content>"""

    for suffix, payload in (
        ("odp", build_odf_package("odp", odp_content)),
        ("odt", build_odf_package("odt", odt_content)),
    ):
        middle, model = analyze_native_test_document(payload, file_suffix=suffix)  # type: ignore[arg-type]
        flattened = [
            inline_text(block["content"])
            for page in model.pages
            for block in page
            if block.get("type") in {BlockType.DOC_TITLE, BlockType.PAGE_FOOTNOTE}
        ]
        markdown = render_markdown(middle)
        docx = render_docx(middle)
        with ZipFile(BytesIO(docx)) as package:
            relationships = package.read("word/_rels/document.xml.rels").decode("utf-8")

        assert flattened
        assert all("<hyperlink>" in content for content in flattened)
        assert "](javascript:alert(1))" not in markdown
        assert "javascript:alert(1)" not in relationships


@pytest.mark.parametrize(
    ("weight", "expected_bold"),
    [
        ("normal", False),
        ("bold", True),
        ("599", False),
        ("600", True),
        ("1000", True),
        ("1001", False),
        ("9" * 100_000, False),
    ],
    ids=("normal", "bold", "below-threshold", "threshold", "upper-bound", "out-of-range", "overlong"),
)
def test_odf_font_weight_is_bounded_before_numeric_conversion(weight: str, expected_bold: bool) -> None:
    """Verify that numeric weights are only converted within the limited CSS range, and no interpreter protection is relied upon for very long inputs."""
    content = f"""<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0">
 <office:automatic-styles><style:style style:name="W" style:family="text">
  <style:text-properties fo:font-weight="{weight}"/>
 </style:style></office:automatic-styles>
 <office:body><office:text><text:p><text:span text:style-name="W">weighted</text:span></text:p>
 </office:text></office:body></office:document-content>"""
    previous_limit = sys.get_int_max_str_digits() if hasattr(sys, "get_int_max_str_digits") else None
    if hasattr(sys, "set_int_max_str_digits"):
        sys.set_int_max_str_digits(0)
    try:
        pages = OdtModel().predict(BytesIO(build_odf_package("odt", content)))
    finally:
        if previous_limit is not None:
            sys.set_int_max_str_digits(previous_limit)

    expected_content = inline("weighted", styles=["bold"] if expected_bold else None)
    assert pages == [[{"type": BlockType.TEXT, "content": expected_content}]]


def test_odf_inline_image_alt_keeps_literal_hyperlink_text_inert() -> None:
    """Verify inline image title/desc Preserves the original text of the label appearance but does not generate active links."""
    literal = "&lt;hyperlink&gt;&lt;text&gt;click&lt;/text&gt;&lt;url&gt;javascript:alert(1)&lt;/url&gt;&lt;/hyperlink&gt;"
    content = f"""<office:document-content
     xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
     xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
     xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
     xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
     xmlns:xlink="http://www.w3.org/1999/xlink">
     <office:body><office:text><text:p>Before <draw:frame>
      <draw:image xlink:href="Pictures/pixel.png"/><svg:title>{literal}</svg:title>
     </draw:frame> After</text:p></office:text></office:body></office:document-content>"""
    payload = build_odf_package("odt", content, extra_parts={"Pictures/pixel.png": _PIXEL_PNG})
    middle, model = analyze_native_test_document(payload, file_suffix="odt")

    raw_content = model.pages[0][0]["content"]
    markdown = render_markdown(middle)
    docx = render_docx(middle)
    with ZipFile(BytesIO(docx)) as package:
        relationships = package.read("word/_rels/document.xml.rels").decode("utf-8")

    assert "<hyperlink>" in inline_text(raw_content)
    assert all(span.get("type") == "text" for span in raw_content)
    assert "](javascript:" not in markdown
    assert "javascript:alert(1)" not in relationships


@pytest.mark.parametrize(
    ("href", "extra_parts"),
    [
        ("Pictures/missing.png", None),
        ("https://example.com/external.png", None),
        ("Pictures/corrupt.png", {"Pictures/corrupt.png": b"not-an-image"}),
    ],
)
def test_odf_unavailable_image_preserves_safe_alt_text(
    href: str,
    extra_parts: dict[str, bytes] | None,
) -> None:
    """Verify that title/desc semantics are preserved in safe text when missing, external, or corrupted images cannot be materialized."""
    literal = "&lt;hyperlink&gt;&lt;text&gt;click&lt;/text&gt;&lt;url&gt;javascript:alert(1)&lt;/url&gt;&lt;/hyperlink&gt;"
    content = f"""<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
 xmlns:xlink="http://www.w3.org/1999/xlink">
 <office:body><office:text><draw:frame><draw:image xlink:href="{href}"/>
  <svg:title>{literal}</svg:title><svg:desc>semantic description</svg:desc>
 </draw:frame></office:text></office:body>
</office:document-content>"""

    middle, model = analyze_native_test_document(build_odf_package("odt", content, extra_parts=extra_parts), file_suffix="odt")
    raw_content = model.pages[0][0]["content"]
    markdown = render_markdown(middle)

    assert model.pages[0][0]["type"] == BlockType.TEXT
    assert "<hyperlink>" in inline_text(raw_content)
    assert all(span.get("type") == "text" for span in raw_content)
    assert "semantic description" in inline_text(raw_content)
    assert "](javascript:" not in markdown


_VECTOR_LOGO_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="118" height="30" viewBox="0 0 118 30">'
    b'<rect width="118" height="30" fill="#006633"/><circle cx="15" cy="15" r="8" fill="#ffffff"/></svg>'
)


@pytest.mark.parametrize(
    ("svg_part", "expected_prefix"),
    [
        (_VECTOR_LOGO_SVG, "data:image/png;base64,"),
        (b"<svg><unclosed>", "data:image/jpeg;base64,"),
    ],
)
def test_odf_svg_image_rasterizes_or_falls_back_to_placeholder(svg_part: bytes, expected_prefix: str) -> None:
    """The SVG image in the verification package is rasterized into PNG according to the frame size, and falls back to the safety placeholder image when it cannot be rendered."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
 xmlns:xlink="http://www.w3.org/1999/xlink">
 <office:body><office:text><draw:frame svg:width="2.82529in" svg:height="0.70632in">
  <draw:image xlink:href="media/logo.svg"/><svg:title>vector logo</svg:title>
 </draw:frame></office:text></office:body>
</office:document-content>"""
    payload = build_odf_package("odt", content, extra_parts={"media/logo.svg": svg_part})

    middle, model = analyze_native_test_document(payload, file_suffix="odt")

    block = model.pages[0][0]
    assert block["type"] == BlockType.IMAGE
    assert isinstance(block["image_base64"], str)
    assert block["image_base64"].startswith(expected_prefix)


def test_odf_word_formula_number_separator_becomes_tag() -> None:
    """Verify that the Word formula number separator # is converted to \\tag instead of a bare # in the ODT formula object."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
 xmlns:xlink="http://www.w3.org/1999/xlink">
 <office:body><office:text><text:p><draw:frame svg:width="1.425in" svg:height="0.175in">
  <draw:object xlink:href="./Object 1"/>
 </draw:frame></text:p></office:text></office:body>
</office:document-content>"""
    formula = (
        b'<math xmlns="http://www.w3.org/1998/Math/MathML" display="block"><mtable><mtr><mtd>'
        b"<mi>E</mi><mo>=</mo><msup><mi>mc</mi><mn>2</mn></msup>"
        b'<mo>#</mo><mo fence="false">(</mo><mn>1</mn><mo fence="false">)</mo>'
        b"</mtd></mtr></mtable></math>"
    )
    payload = build_odf_package("odt", content, extra_parts={"Object 1/content.xml": formula})

    middle, model = analyze_native_test_document(payload, file_suffix="odt")

    block = model.pages[0][0]
    assert block["type"] == BlockType.EQUATION
    assert block["content"] == r"E={mc}^{2}\tag{1}"


def test_odf_annotation_emits_page_footnote_without_metadata_in_body() -> None:
    """Verification Criteria annotation The text is retained as a footer and the author's date is not spelled out into the surrounding text."""
    content = """<office:document-content
     xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
     xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
     xmlns:dc="http://purl.org/dc/elements/1.1/">
     <office:body><office:text><text:p>Before <office:annotation office:name="comment-1">
      <dc:creator>Alice</dc:creator><dc:date>2026-01-01</dc:date><text:p>Review note</text:p>
     </office:annotation>After<office:annotation-end office:name="comment-1"/></text:p>
     </office:text></office:body></office:document-content>"""

    pages = OdtModel().predict(BytesIO(build_odf_package("odt", content)))

    body = next(block for block in pages[0] if block["type"] == BlockType.TEXT)
    annotation = next(block for block in pages[0] if block["type"] == BlockType.PAGE_FOOTNOTE)
    assert inline_text(body["content"]) == "Before After"
    assert inline_text(annotation["content"]) == "Review note"
    assert "Alice" not in str(pages) and "2026-01-01" not in str(pages)


def test_ods_sheet_titles_escape_literal_inline_protocol() -> None:
    """Verifying multiple sheet headers does not reconstruct the internal protocol in the name as an active link."""
    literal = "&lt;hyperlink&gt;&lt;text&gt;x&lt;/text&gt;&lt;url&gt;javascript:alert(1)&lt;/url&gt;&lt;/hyperlink&gt;"
    content = f"""<office:document-content
     xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
     xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
     xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
     <office:body><office:spreadsheet>
      <table:table table:name="{literal}"><table:table-row><table:table-cell>
       <text:p>A</text:p>
      </table:table-cell></table:table-row></table:table>
      <table:table table:name="Safe"><table:table-row><table:table-cell>
       <text:p>B</text:p>
      </table:table-cell></table:table-row></table:table>
     </office:spreadsheet></office:body></office:document-content>"""
    middle, model = analyze_native_test_document(build_odf_package("ods", content), file_suffix="ods")

    title = model.pages[0][0]["content"]
    markdown = render_markdown(middle)
    docx = render_docx(middle)
    with ZipFile(BytesIO(docx)) as package:
        relationships = package.read("word/_rels/document.xml.rels").decode("utf-8")

    assert inline_text(title).startswith("<hyperlink>")
    assert all(span.get("type") == "text" for span in title)
    assert "](javascript:" not in markdown
    assert "javascript:alert(1)" not in relationships


def test_odp_skips_hidden_drawing_page_styles_in_output_and_metadata() -> None:
    """Verification ODP converter shares drawing-page visibility resolution with metadata."""
    content = """<office:document-content
     xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
     xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
     xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"
     xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
     xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
     <office:automatic-styles>
      <style:style style:name="HiddenPage" style:family="drawing-page">
       <style:drawing-page-properties presentation:visibility="hidden"/>
      </style:style>
     </office:automatic-styles>
     <office:body><office:presentation>
      <draw:page draw:name="Visible"><draw:frame><draw:text-box>
       <text:p>visible slide</text:p>
      </draw:text-box></draw:frame></draw:page>
      <draw:page draw:name="StyledHidden" draw:style-name="HiddenPage"><draw:frame><draw:text-box>
       <text:p>styled hidden slide</text:p>
      </draw:text-box></draw:frame></draw:page>
      <draw:page draw:name="DirectHidden" presentation:visibility="hidden"><draw:frame><draw:text-box>
       <text:p>direct hidden slide</text:p>
      </draw:text-box></draw:frame></draw:page>
     </office:presentation></office:body></office:document-content>"""
    payload = build_odf_package("odp", content)

    pages = OdpModel().predict(BytesIO(payload))
    metadata = extract_odf_metadata(BytesIO(payload), "odp")

    assert len(pages) == 1
    assert "visible slide" in str(pages)
    assert "hidden slide" not in str(pages)
    assert metadata["page_count"] == 1


def test_odf_style_cycle_is_bounded_and_preserves_text() -> None:
    """Verification loop parent-style-name degrades within limited links without blocking text parsing."""
    content = """<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0">
 <office:body><office:text><text:p text:style-name="A">visible</text:p></office:text></office:body>
</office:document-content>"""
    styles = """<office:document-styles
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0">
 <office:styles><style:style style:name="A" style:family="paragraph" style:parent-style-name="B"/>
  <style:style style:name="B" style:family="paragraph" style:parent-style-name="A"/>
 </office:styles></office:document-styles>"""
    assert OdtModel().predict(BytesIO(build_odf_package("odt", content, styles_xml=styles))) == [
        [{"type": BlockType.TEXT, "content": inline("visible")}]
    ]


def test_odf_package_rejects_unsafe_member_paths_and_dtd() -> None:
    """Verification of ZIP jump-up members and XML DTD failed before entering semantic parsing."""
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as package:
        package.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        package.writestr("../escape", b"unsafe")
    with pytest.raises(OdfParseError, match="unsafe member path"):
        OdfPackage(output.getvalue())

    dtd_content = """<!DOCTYPE doc [<!ENTITY x "hidden">]>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0">
 <office:body><office:text/></office:body></office:document-content>"""
    with pytest.raises(OdfParseError, match="DTD is not allowed"):
        OdtModel().predict(BytesIO(build_odf_package("odt", dtd_content)))


def test_csv_and_rtf_runtime_do_not_load_odf_modules() -> None:
    """Verify that the new ODF converter does not enter the lazy import boundary of the existing CSV/RTF."""
    script = "\n".join(
        [
            "import io, sys",
            "from docvortex.analyzers.native import CsvModel, RtfModel",
            "CsvModel().predict(io.BytesIO(b'a,b\\n1,2\\n'))",
            "RtfModel().predict(io.BytesIO(b'{\\\\rtf1 ok}'))",
            "assert not any(name.startswith('docvortex.analyzers.native.office.odf') for name in sys.modules)",
        ]
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_odf_subpackage_does_not_export_models() -> None:
    """Verify that the ODF model is only exposed from the Flash root package and does not form a second set of public paths."""
    assert importlib.util.find_spec("docvortex.analyzers.native.office.odf.model") is None
    package = __import__("docvortex.analyzers.native.office.odf", fromlist=["__all__"])
    assert package.__all__ == []
