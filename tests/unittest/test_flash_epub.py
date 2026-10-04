from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest
from _epub_test_utils import (
    build_epub2_fixture,
    build_epub_fixture,
    build_epub_notes_fixture,
    build_epub_table_toc_fixture,
)
from _native_test_utils import analyze_native_test_document
from _span_test_utils import inline, inline_text, inline_urls, visible_content
from bs4 import BeautifulSoup
from loguru import logger
from lxml import etree

import docvortex.analyzers.native.epub.package as epub_package_module
from docvortex.analyzers.native import EpubModel
from docvortex.analyzers.native._shared.markup import MarkupStylesheet, TextStyle
from docvortex.analyzers.native.epub import EpubEncryptedError, EpubPackage, EpubParseError, EpubResourceLimitError, detect_epub
from docvortex.analyzers.native.epub.xhtml import EpubChapterConverter, build_anchor_registry, convert_svg_spine
from docvortex.api import parse
from docvortex.document.detection import guess_suffix_by_bytes, guess_suffix_by_path
from docvortex.postprocess.lists import fix_office_list_blocks
from docvortex.render import RenderMode, render_docx, render_html, render_markdown, render_structured_content
from docvortex.schema import BlockType, PageFootnoteBlock


def test_epub_notes_use_page_footnote_and_document_wide_anchors() -> None:
    """Verify unified semantics for Footnote/Endnote, ARIA role, duplicate ID and cross-chapter noteref."""
    middle, model = analyze_native_test_document(build_epub_notes_fixture(), file_suffix="epub")
    blocks = [block for page in middle.pages for block in page.blocks]
    footnotes = [block for block in blocks if block.type == BlockType.PAGE_FOOTNOTE]
    assert all(isinstance(block, PageFootnoteBlock) for block in footnotes)
    footnote_by_text = {inline_text(block.content): block for block in footnotes}  # type: ignore[union-attr]

    first = footnote_by_text["First footnote paragraph back."]
    second = footnote_by_text["Second footnote paragraph."]
    endnote = footnote_by_text["First endnote paragraph."]
    duplicate_first = footnote_by_text["First duplicate note."]
    duplicate_second = footnote_by_text["Second duplicate note."]
    assert first.anchor and endnote.anchor and duplicate_first.anchor  # type: ignore[union-attr]
    assert first.anchor == "epub-5da65ec1e273b731b44c"  # type: ignore[union-attr]
    assert endnote.anchor == "epub-b7ab2fcf9373c09c71d6"  # type: ignore[union-attr]
    assert duplicate_first.anchor == "epub-2bd2576dd7c5d3cf58f4"  # type: ignore[union-attr]
    assert duplicate_second.anchor == "epub-2b154ee53d6a59bb32f7"  # type: ignore[union-attr]
    assert second.anchor is None  # type: ignore[union-attr]
    assert duplicate_second.anchor and duplicate_second.anchor != duplicate_first.anchor  # type: ignore[union-attr]
    assert footnote_by_text["ARIA footnote."].anchor  # type: ignore[union-attr]
    assert footnote_by_text["ARIA endnote."].anchor  # type: ignore[union-attr]
    assert footnote_by_text["Legacy rearnote."].anchor  # type: ignore[union-attr]
    assert footnote_by_text["Anonymous endnote."].anchor  # type: ignore[union-attr]

    body_text = next(
        block
        for block in middle.pages[0].blocks
        if block.type == BlockType.TEXT and "Same-page" in inline_text(block.content)  # type: ignore[union-attr]
    )
    assert f"#{first.anchor}" in inline_urls(body_text.content)  # type: ignore[union-attr]
    assert f"#{endnote.anchor}" in inline_urls(body_text.content)  # type: ignore[union-attr]
    assert f"#{duplicate_first.anchor}" in inline_urls(body_text.content)  # type: ignore[union-attr]
    assert "empty [4]" in inline_text(body_text.content)  # type: ignore[union-attr]
    assert body_text.anchor
    assert inline_urls(first.content) == [f"#{body_text.anchor}"]  # type: ignore[union-attr]

    assert any(block.type == BlockType.TEXT and inline_text(block.content) == "Ordinary aside." for block in blocks)  # type: ignore[union-attr]
    assert any(block.type == BlockType.TEXT and inline_text(block.content) == "Footnotes collection label." for block in blocks)  # type: ignore[union-attr]
    assert any(block.type == BlockType.LIST and "Footnote sibling list" in str(block.content) for block in blocks)  # type: ignore[union-attr]
    assert any(block.type == BlockType.TABLE and "Complex only" in str(block.content) for block in blocks)  # type: ignore[union-attr]
    assert all(
        block.get("type") != BlockType.REF_TEXT
        for page in model.pages
        for block in page
        if "footnote" in str(block.get("content", "")).casefold() or "endnote" in str(block.get("content", "")).casefold()
    )

    default_markdown = render_markdown(middle)
    full_markdown = render_markdown(middle, mode=RenderMode.FULL)
    assert "First footnote paragraph" in default_markdown
    assert f"](#{first.anchor})" in default_markdown
    assert f'id="{first.anchor}" class="docvortex-page-footnote"' in default_markdown
    assert f'id="{first.anchor}" class="docvortex-page-footnote"' in full_markdown
    assert "Page footnote:" not in default_markdown

    for html_output in (
        render_html(middle, standalone=False),
        render_html(middle, mode=RenderMode.FULL, standalone=False),
    ):
        assert f'href="#{first.anchor}"' in html_output
        assert f'id="{first.anchor}"' in html_output
        assert 'class="docvortex-page-footnote"' in html_output

    structured = render_structured_content(middle)
    structured_blocks = [block for page in structured["pages"] for block in page["blocks"]]
    structured_footnote = next(block for block in structured_blocks if block.get("anchor") == first.anchor)
    assert structured_footnote["type"] == BlockType.PAGE_FOOTNOTE
    assert structured_footnote["content"] == f"First footnote paragraph [back](#{body_text.anchor})."
    assert any(f"](#{first.anchor})" in str(block.get("content", "")) for block in structured_blocks)


def test_epub_internal_links_and_lists_use_cross_renderer_projection() -> None:
    """Verify that the spine link is preserved, the list specification is consecutive Arabic numbers and the directory page is not injected."""
    middle, _ = analyze_native_test_document(build_epub_fixture(), file_suffix="epub")
    first_page = middle.pages[0]
    second_page = middle.pages[1]
    first_title = first_page.blocks[0]
    second_title = second_page.blocks[0]
    assert first_title.anchor and second_title.anchor  # type: ignore[union-attr]
    assert first_title.anchor == "epub-1daacccca5bf43833643"  # type: ignore[union-attr]
    assert second_title.anchor == "epub-8d7fe965e2d714cf08ae"  # type: ignore[union-attr]
    markdown = render_markdown(middle)
    assert markdown.startswith(f'<a id="{first_title.anchor}"></a>\n# Chapter One')  # type: ignore[union-attr]
    assert "NAV Chapter One" not in markdown
    assert "NCX Chapter One" not in markdown
    assert "Landmark" not in markdown
    assert f"](#{second_title.anchor})" in markdown  # type: ignore[union-attr]
    assert f"](#{first_title.anchor})" in markdown  # type: ignore[union-attr]
    assert "3. Three" in markdown
    assert "4. Two" in markdown
    list_block = next(block for block in first_page.blocks if block.type == BlockType.LIST)
    assert [inline_text(child.content) for child in list_block.content] == ["3. Three", "4. Two"]
    assert all(label in render_html(middle, standalone=False) for label in ("Three", "Two"))
    structured = render_structured_content(middle)
    assert any(block.get("content") == "3. Three\n4. Two" for page in structured["pages"] for block in page["blocks"])
    assert render_docx(middle).startswith(b"PK")


def test_epub_hidden_list_items_do_not_consume_normalized_numbers() -> None:
    """Verify that hidden entries are filtered first, unified list post-processing and then visible content is numbered consecutively."""
    package = EpubPackage(build_epub_fixture())
    chapter_path = "EPUB/text/ch1.xhtml"
    try:
        root = package.xml_part(chapter_path, allow_external_doctype=True)
        ordered_list = next(element for element in root.iter() if isinstance(element.tag, str) and element.tag.endswith("}ol"))
        ordered_list.attrib.pop("start", None)
        ordered_list.attrib.pop("reversed", None)
        ordered_list.attrib.pop("type", None)
        items = [child for child in ordered_list if isinstance(child.tag, str) and child.tag.endswith("}li")]
        items[0].set("hidden", "hidden")
        anchors = build_anchor_registry([(chapter_path, root)], package)
        blocks = EpubChapterConverter(package, chapter_path, root, anchors).convert()
        list_block = next(block for block in blocks if block["type"] == BlockType.LIST)

        assert list_block["start"] == 1
        assert list_block["content"] == [{"type": BlockType.TEXT, "content": inline("Two")}]
        fix_office_list_blocks([list_block])
        assert inline_text(list_block["content"][0]["content"]) == "1. Two"
    finally:
        package.close()


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("hidden", "hidden"),
        ("aria-hidden", "true"),
        ("style", "display: none"),
        ("class", "hidden"),
    ],
)
def test_epub_anchor_registry_excludes_hidden_headings(attribute: str, value: str) -> None:
    """Verify that registry does not generate dangling anchor for hidden headers that converter would discard."""
    package = EpubPackage(build_epub_fixture())
    chapter_path = "EPUB/text/ch1.xhtml"
    try:
        root = package.xml_part(chapter_path, allow_external_doctype=True)
        heading = next(element for element in root.iter() if isinstance(element.tag, str) and element.tag.endswith("}h1"))
        heading.set(attribute, value)
        heading.text = "Hidden target"
        body = next(element for element in root.iter() if isinstance(element.tag, str) and element.tag.endswith("}body"))
        namespace = etree.QName(body).namespace
        paragraph = etree.SubElement(body, f"{{{namespace}}}p")
        link = etree.SubElement(paragraph, f"{{{namespace}}}a", href="#chapter-one")
        link.text = "Hidden link"

        anchors = build_anchor_registry([(chapter_path, root)], package)
        blocks = EpubChapterConverter(package, chapter_path, root, anchors).convert()

        assert anchors.resolve_link("#chapter-one", base_part=chapter_path) is None
        assert "Hidden target" not in str(blocks)
        assert "Hidden link" in str(blocks)
        assert "<hyperlink>Hidden link" not in str(blocks)
    finally:
        package.close()


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("hidden", "hidden"),
        ("aria-hidden", "true"),
        ("style", "display: none"),
        ("class", "hidden"),
    ],
)
def test_epub_anchor_registry_excludes_hidden_notes(attribute: str, value: str) -> None:
    """Validation Hidden note does not generate converter Uncashed fragment target."""
    package = EpubPackage(build_epub_notes_fixture())
    try:
        chapters: list[tuple[str, etree._Element]] = []
        note_path = ""
        hidden_note: etree._Element | None = None
        for item in package.spine:
            if not item.path or not item.media_type or "xhtml" not in item.media_type:
                continue
            root = package.xml_part(item.path, allow_external_doctype=True)
            chapters.append((item.path, root))
            for element in root.iter():
                if element.get("id") == "fn-one":
                    hidden_note = element
                    note_path = item.path
        assert hidden_note is not None
        hidden_note.set(attribute, value)
        if attribute == "class":
            namespace = etree.QName(hidden_note.getroottree().getroot()).namespace
            style = etree.SubElement(hidden_note.getroottree().getroot(), f"{{{namespace}}}style")
            style.text = ".hidden { display: none; }"

        anchors = build_anchor_registry(chapters, package)
        blocks = [
            block
            for chapter_path, root in chapters
            for block in EpubChapterConverter(package, chapter_path, root, anchors).convert()
        ]

        anchors.finalize_links([blocks])
        assert anchors.resolve_link("#fn-one", base_part=note_path) is None
        assert "First footnote paragraph" not in str(blocks)
        assert "Same-page [1]" in str(blocks)
        assert "<hyperlink>[1]" not in str(blocks)
    finally:
        package.close()


def test_epub_table_contents_resolve_title_marker_and_expand_only_exact_single_target_rows() -> None:
    """Verify that header internal marker is jumpable and that the table of contents only expands the strictly matched single target row."""
    middle, model = analyze_native_test_document(build_epub_table_toc_fixture(), file_suffix="epub")
    assert len(middle.pages) == 2
    assert all(block.type != BlockType.INDEX for page in middle.pages for block in page.blocks)
    table = next(block for block in model.pages[0] if block["type"] == BlockType.TABLE)
    soup = BeautifulSoup(str(table["content"]), "html.parser")
    rows = soup.find_all("tr")

    positive_links = rows[0].find_all("a", href=True)
    assert [link.get_text(strip=True) for link in positive_links] == ["CHAPTER I.", "Target Chapter"]
    assert len({link["href"] for link in positive_links}) == 1
    assert rows[1].find_all("a", href=True)[0].get_text(strip=True) == "Mismatch"
    assert len(rows[1].find_all("a", href=True)) == 1
    assert rows[2].find("a", href=True)["href"] == "https://example.com"  # type: ignore[index]
    assert len(rows[2].find_all("a", href=True)) == 1
    assert len(rows[3].find_all("a", href=True)) == 2

    rendered = BeautifulSoup(render_html(middle, standalone=False), "html.parser")
    chapter_link = rendered.find("a", string="Target Chapter")
    chapter_title = rendered.find(id=positive_links[0]["href"].removeprefix("#"))
    assert chapter_link is not None and chapter_title is not None
    assert chapter_link["href"] == f"#{chapter_title['id']}"


def test_epub_table_spans_are_bounded_before_downstream_grid_parsing() -> None:
    """Verify that the oversized EPUB colspan is removed before entering the DOCX placeholder grid."""
    package = EpubPackage(build_epub_fixture())
    chapter_path = "EPUB/text/ch2.xhtml"
    try:
        root = package.xml_part(chapter_path, allow_external_doctype=True)
        cell = next(element for element in root.iter() if isinstance(element.tag, str) and element.tag.endswith("}td"))
        cell.set("colspan", "100000000")

        anchors = build_anchor_registry([(chapter_path, root)], package)
        blocks = EpubChapterConverter(package, chapter_path, root, anchors).convert()
        table = next(block for block in blocks if block["type"] == BlockType.TABLE)
        parsed_cell = BeautifulSoup(str(table["content"]), "html.parser").find("td", string="1")

        assert parsed_cell is not None
        assert parsed_cell["rowspan"] == "2"
        assert not parsed_cell.has_attr("colspan")
    finally:
        package.close()


def _build_epub_with_svg_image_fixture() -> bytes:
    """Constructs a minimal EPUB 3 with a picture reference in the SVG package."""
    container = """<?xml version="1.0" encoding="UTF-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""
    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">urn:uuid:svg-image</dc:identifier></metadata>
  <manifest>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
    <item id="logo" href="images/logo.svg" media-type="image/svg+xml"/>
  </manifest>
  <spine><itemref idref="chapter"/></spine>
</package>"""
    chapter = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Logo</h1>
  <p><img src="images/logo.svg" alt="Vector logo"/></p>
</body></html>"""
    logo = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
        b'<rect width="40" height="20" fill="green"/></svg>'
    )
    output = BytesIO()
    with ZipFile(output, "w") as package:
        package.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        package.writestr("META-INF/container.xml", container, compress_type=ZIP_DEFLATED)
        package.writestr("OEBPS/content.opf", opf, compress_type=ZIP_DEFLATED)
        package.writestr("OEBPS/chapter.xhtml", chapter, compress_type=ZIP_DEFLATED)
        package.writestr("OEBPS/images/logo.svg", logo, compress_type=ZIP_DEFLATED)
    return output.getvalue()


def test_epub_svg_package_image_rasterizes_to_png() -> None:
    """Verify that the SVG image in the package is rasterized into PNG instead of discarding the entire image and leaving only alt."""
    package = EpubPackage(_build_epub_with_svg_image_fixture())
    chapter_path = "OEBPS/chapter.xhtml"
    try:
        root = package.xml_part(chapter_path)
        anchors = build_anchor_registry([(chapter_path, root)], package)
        blocks = EpubChapterConverter(package, chapter_path, root, anchors).convert()

        image = next(block for block in blocks if block["type"] == BlockType.IMAGE)
        assert image["image_base64"].startswith("data:image/png;base64,")
    finally:
        package.close()


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("hidden", "hidden"),
        ("aria-hidden", "true"),
        ("style", "display: none"),
        ("class", "hidden"),
    ],
)
def test_epub_figure_skips_hidden_direct_images(attribute: str, value: str) -> None:
    """Verify that direct images of figure also obey the element attributes, inline styles, and CSS hiding rules."""
    package = EpubPackage(build_epub_fixture())
    chapter_path = "EPUB/text/ch1.xhtml"
    try:
        root = package.xml_part(chapter_path, allow_external_doctype=True)
        figure = next(element for element in root.iter() if isinstance(element.tag, str) and element.tag.endswith("}figure"))
        image = next(child for child in figure if isinstance(child.tag, str) and child.tag.endswith("}img"))
        image.set(attribute, value)

        anchors = build_anchor_registry([(chapter_path, root)], package)
        blocks = EpubChapterConverter(package, chapter_path, root, anchors).convert()

        assert all(block["type"] != BlockType.IMAGE for block in blocks)
        assert "Dot caption" in str(blocks)
    finally:
        package.close()


def test_epub_figure_preserves_direct_text_and_child_tails() -> None:
    """Verify sharing projector does not discard direct text and pictures of XHTML figure, caption tail."""
    package = EpubPackage(build_epub_fixture())
    chapter_path = "EPUB/text/ch1.xhtml"
    try:
        root = package.xml_part(chapter_path, allow_external_doctype=True)
        figure = next(element for element in root.iter() if isinstance(element.tag, str) and element.tag.endswith("}figure"))
        image = next(child for child in figure if isinstance(child.tag, str) and child.tag.endswith("}img"))
        caption = next(child for child in figure if isinstance(child.tag, str) and child.tag.endswith("}figcaption"))
        figure.text = "Before"
        image.tail = "After"
        caption.tail = "Tail"

        anchors = build_anchor_registry([(chapter_path, root)], package)
        blocks = EpubChapterConverter(package, chapter_path, root, anchors).convert()
        relevant = [
            block
            for block in blocks
            if block.get("type") in {BlockType.IMAGE, BlockType.IMAGE_CAPTION}
            or visible_content(block.get("content")) in {"Before", "AfterTail"}
        ]

        assert [(block["type"], visible_content(block.get("content"))) for block in relevant] == [
            (BlockType.TEXT, "Before"),
            (BlockType.IMAGE, ""),
            (BlockType.IMAGE_CAPTION, "Dot caption"),
            (BlockType.TEXT, "AfterTail"),
        ]
    finally:
        package.close()


def test_epub2_and_mimetype_less_compatibility_packages_parse() -> None:
    """Verify EPUB 2 common subset and container compatible branches missing mimetype."""
    epub2 = build_epub2_fixture()
    compatibility = build_epub_fixture(omit_mimetype=True)
    assert detect_epub(epub2)
    assert detect_epub(compatibility)
    epub2_middle = analyze_native_test_document(epub2, file_suffix="epub")[0]
    assert inline_text(epub2_middle.pages[0].blocks[0].content) == "EPUB Two Chapter"  # type: ignore[union-attr]
    assert inline_text(epub2_middle.pages[0].blocks[1].content) == "Legacy\u00a0body"  # type: ignore[union-attr]
    assert len(analyze_native_test_document(compatibility, file_suffix="epub")[0].pages) == 3


def test_epub_mimetype_with_surrounding_whitespace_parses() -> None:
    """Verify that mimetype real-world EPUB with leading and trailing whitespace is parsable, error mimetype is still rejected."""
    sloppy = build_epub_fixture(mimetype_value="application/epub+zip\r\n")
    assert detect_epub(sloppy)
    assert len(analyze_native_test_document(sloppy, file_suffix="epub")[0].pages) == 3
    invalid = build_epub_fixture(mimetype_value="application/zip")
    with pytest.raises(EpubParseError, match="invalid mimetype"):
        EpubPackage(invalid)


def test_epub_spine_foreign_resource_uses_xhtml_fallback_chain() -> None:
    """Verify foreign spine item is missing along with manifest fallback to XHTML."""
    middle, _ = analyze_native_test_document(build_epub_fixture(use_foreign_fallback=True), file_suffix="epub")
    assert inline_text(middle.pages[1].blocks[0].content) == "Section Two"  # type: ignore[union-attr]


def test_epub_spine_missing_supported_resource_uses_xhtml_fallback_chain() -> None:
    """Verify that the missing XHTML master resource does not block the available fallback chain."""
    middle, _ = analyze_native_test_document(build_epub_fixture(use_missing_supported_fallback=True), file_suffix="epub")
    assert inline_text(middle.pages[1].blocks[0].content) == "Section Two"  # type: ignore[union-attr]


def test_epub_nav_and_ncx_outside_spine_do_not_create_synthetic_page() -> None:
    """Verify that neither a corrupted nav nor a valid NCX other than spine will generate a composite directory page."""
    middle, _ = analyze_native_test_document(build_epub_fixture(corrupt_nav=True), file_suffix="epub")
    markdown = render_markdown(middle)
    assert len(middle.pages) == 3
    assert all(block.type != BlockType.INDEX for page in middle.pages for block in page.blocks)
    assert "NCX Chapter One" not in markdown
    assert "NAV Chapter One" not in markdown


def test_epub_without_authored_toc_keeps_only_spine_pages_and_titles() -> None:
    """Verify that extra table of contents pages are not generated from text headers when nav/NCX is missing."""
    middle, _ = analyze_native_test_document(build_epub_fixture(include_nav=False, include_ncx=False), file_suffix="epub")
    assert len(middle.pages) == 3
    assert middle.pages[0].blocks[0].type == BlockType.DOC_TITLE
    assert middle.pages[1].blocks[0].type == BlockType.PARAGRAPH_TITLE
    assert all(block.type != BlockType.INDEX for page in middle.pages for block in page.blocks)


def test_epub_without_toc_or_headings_does_not_add_empty_page() -> None:
    """Verify that an empty IndexBlock page is not generated when there are no valid directory entries."""
    middle, _ = analyze_native_test_document(
        build_epub_fixture(include_nav=False, include_ncx=False, strip_headings=True), file_suffix="epub"
    )
    assert len(middle.pages) == 3
    assert middle.pages[0].blocks[0].type != BlockType.INDEX


def test_epub_navigation_in_spine_preserves_page_order_and_extra_body_content() -> None:
    """Verify that nav in spine retains the table of contents and extra text as a normal XHTML page."""
    middle, _ = analyze_native_test_document(
        build_epub_fixture(nav_in_spine=True, nav_extra_body_text="Publisher front matter"), file_suffix="epub"
    )
    assert len(middle.pages) == 4
    assert all(block.type != BlockType.INDEX for page in middle.pages for block in page.blocks)
    assert any(inline_text(getattr(block, "content", None)) == "Publisher front matter" for block in middle.pages[0].blocks)
    assert inline_text(middle.pages[1].blocks[0].content) == "Chapter One"  # type: ignore[union-attr]


def test_epub_content_detection_precedes_extension_and_rejects_fake_packages(tmp_path: Path) -> None:
    """Verification EPUB strong content identity overrides masquerade extensions, whereas plain ZIP/text cannot rely on extensions to pass."""
    payload = build_epub_fixture()
    disguised = tmp_path / "book.csv"
    disguised.write_bytes(payload)
    assert guess_suffix_by_bytes(payload, str(disguised)) == "epub"
    assert guess_suffix_by_path(disguised) == "epub"

    fake = tmp_path / "fake.epub"
    fake.write_text("not an epub", encoding="utf-8")
    assert guess_suffix_by_path(fake) != "epub"
    with pytest.raises(ValueError, match="Unsupported native input format: txt"):
        parse(fake)


def test_epub_corrupt_chapter_keeps_empty_spine_placeholder() -> None:
    """Verify that local XHTML damage does not move subsequent spine page numbers."""
    middle, model = analyze_native_test_document(build_epub_fixture(corrupt_second_chapter=True), file_suffix="epub")
    assert len(model.pages) == 3
    assert model.pages[1] == []
    assert [page.page_idx for page in middle.pages] == [0, 1, 2]
    assert middle.pages[1].blocks == []
    assert "SVG text" in inline_text(middle.pages[2].blocks[0].content)  # type: ignore[union-attr]


def test_epub_malformed_xhtml_warns_and_recovers_without_relaxing_xml_part() -> None:
    """The alarm is restored after verification text XHTML strict fails, while the generic XML entry remains strict."""
    payload = build_epub_fixture(unclosed_br_second_chapter=True)
    package = EpubPackage(payload)
    try:
        assert package.xml_part("EPUB/text/ch2.xhtml", allow_external_doctype=True) is None
        recovered = package.xhtml_part("EPUB/text/ch2.xhtml", allow_external_doctype=True)
        assert recovered is not None
        assert etree.QName(recovered).localname == "html"
    finally:
        package.close()

    warning_messages: list[str] = []
    sink_id = logger.add(lambda message: warning_messages.append(str(message)), level="WARNING", format="{message}")
    try:
        middle, model = analyze_native_test_document(payload, file_suffix="epub")
    finally:
        logger.remove(sink_id)

    assert len(model.pages) == 3
    assert model.pages[1]
    assert "Recovered before" in str(model.pages[1])
    assert "Recovered after" in str(model.pages[1])
    assert middle.pages[1].blocks
    assert any("Recovered malformed EPUB XHTML part path='EPUB/text/ch2.xhtml'" in message for message in warning_messages)


def test_epub_svg_extraction_skips_hidden_descendants_and_hidden_root() -> None:
    """Verification standalone SVG does not extract hidden text, hidden pictures, or hidden ancestor subtrees."""
    package = EpubPackage(build_epub_fixture())
    path = "EPUB/fixed/page.svg"
    try:
        root = package.xml_part(path)
        assert root is not None
        namespace = etree.QName(root).namespace
        original_text = next(root.iter(f"{{{namespace}}}text"))
        original_text.set("style", "display: none")
        original_image = next(root.iter(f"{{{namespace}}}image"))
        original_image.set("style", "visibility: hidden")
        hidden_group = etree.SubElement(root, f"{{{namespace}}}g", style="display: none")
        etree.SubElement(hidden_group, f"{{{namespace}}}text").text = "hidden group text"
        etree.SubElement(root, f"{{{namespace}}}text").text = "visible graphic label"

        blocks = convert_svg_spine(package, path, root)

        assert "visible graphic label" in str(blocks)
        assert "SVG text" not in str(blocks)
        assert "hidden group text" not in str(blocks)
        assert all(block["type"] != BlockType.IMAGE for block in blocks)

        root.set("style", "display: none")
        assert convert_svg_spine(package, path, root) == []
    finally:
        package.close()


def test_epub_malformed_resource_and_link_references_degrade_locally() -> None:
    """Illegal verification URI only discards styles, pictures or link targets, and does not block the chapter text."""
    package = EpubPackage(build_epub_fixture())
    chapter_path = "EPUB/text/ch1.xhtml"
    try:
        root = package.xml_part(chapter_path, allow_external_doctype=True)
        assert root is not None
        stylesheet = next(element for element in root.iter() if etree.QName(element).localname == "link")
        hyperlink = next(element for element in root.iter() if etree.QName(element).localname == "a")
        image = next(element for element in root.iter() if etree.QName(element).localname == "img")
        stylesheet.set("href", "http://[")
        hyperlink.set("href", "http://[")
        image.set("src", "http://[")

        anchors = build_anchor_registry([(chapter_path, root)], package)
        blocks = EpubChapterConverter(package, chapter_path, root, anchors).convert()

        assert "Chapter One" in str(blocks)
        assert "chapter two" in str(blocks)
        assert "Remote image" in str(blocks)
        assert "<hyperlink>chapter two" not in str(blocks)
    finally:
        package.close()


def test_epub_rejects_encrypted_unsafe_and_dtd_inputs() -> None:
    """Verification of selected body encryption, jump-up members, and DTD stable failure before semantic parsing."""
    encrypted = build_epub_fixture(encrypted_paths=("EPUB/text/ch1.xhtml",))
    with pytest.raises(EpubEncryptedError, match="Encrypted EPUB resource"):
        EpubModel().predict(BytesIO(encrypted))

    unsafe = build_epub_fixture(unsafe_member="../escape")
    with pytest.raises(EpubParseError, match="unsafe member path"):
        EpubPackage(unsafe)

    dtd_package = EpubPackage(build_epub_fixture(dtd_first_chapter=True))
    try:
        with pytest.raises(EpubParseError, match="DTD declarations are not allowed"):
            dtd_package.xml_part("EPUB/text/ch1.xhtml")
        with pytest.raises(EpubParseError, match="DTD declarations are not allowed"):
            dtd_package.xhtml_part("EPUB/text/ch1.xhtml", allow_external_doctype=True)
    finally:
        dtd_package.close()

    malformed_dtd_package = EpubPackage(
        build_epub_fixture(
            dtd_first_chapter=True,
            unclosed_br_first_chapter=True,
        )
    )
    try:
        with pytest.raises(EpubParseError, match="DTD declarations are not allowed"):
            malformed_dtd_package.xhtml_part("EPUB/text/ch1.xhtml", allow_external_doctype=True)
    finally:
        malformed_dtd_package.close()


def test_epub_resource_limits_fail_before_semantic_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that ZIP entry and XML depth budget are in effect before text traversal."""
    payload = build_epub_fixture()
    monkeypatch.setattr(epub_package_module, "MAX_ENTRY_BYTES", 32)
    with pytest.raises(EpubResourceLimitError, match="max_entry_bytes"):
        EpubPackage(payload)

    monkeypatch.setattr(epub_package_module, "MAX_ENTRY_BYTES", 128 * 1024 * 1024)
    monkeypatch.setattr(epub_package_module, "MAX_XML_DEPTH", 2)
    with pytest.raises(EpubResourceLimitError, match="max_xml_depth"):
        EpubPackage(payload)


def test_epub_stylesheet_indexes_repeated_selectors_and_preserves_cascade_order() -> None:
    """Verify that duplicate selector is aggregated by attributes, and the interleaving rules with the same priority still comply with the source code order."""
    stylesheet = MarkupStylesheet()
    stylesheet.add(
        ".x { font-weight: bold; display: none; }"
        ".y { font-weight: normal; display: block; }"
        ".x { font-style: italic; }" + ".unused { text-decoration: underline; }" * 10_000
    )
    element = etree.fromstring(b'<span class="x y"/>')

    resolved = stylesheet.resolve(element, TextStyle())

    assert resolved.text.bold is False
    assert resolved.text.italic is True
    assert resolved.hidden is False
    assert len(stylesheet._class_cascades) == 3


def test_epub_stylesheet_honors_important_before_specificity_and_source_order() -> None:
    """Validates that important claims take precedence over subsequent normal rules and normal inline, and allows inline and important overrides."""
    stylesheet = MarkupStylesheet()
    stylesheet.add(
        ".secret { display: none !important; font-weight: bold !important; }.secret { display: block; font-weight: normal; }"
    )

    normal_inline = stylesheet.resolve(
        etree.fromstring(b'<span class="secret" style="display: block; font-weight: normal"/>'),
        TextStyle(),
    )
    important_inline = stylesheet.resolve(
        etree.fromstring(b'<span class="secret" style="display: block !important; font-weight: normal !important"/>'),
        TextStyle(),
    )

    assert normal_inline.hidden is True
    assert normal_inline.text.bold is True
    assert important_inline.hidden is False
    assert important_inline.text.bold is False


@pytest.mark.parametrize(
    ("css", "expected_hidden"),
    [
        (".secret { display: none; visibility: visible; }", True),
        (".secret { display: block; visibility: hidden; }", True),
        (".secret { display: block; visibility: collapse; }", True),
        (
            ".secret { display: none !important; visibility: hidden; }"
            ".secret { display: block; visibility: visible !important; }",
            True,
        ),
        (".secret { display: block; visibility: visible; }", False),
    ],
)
def test_epub_stylesheet_tracks_display_and_visibility_independently(css: str, expected_hidden: bool) -> None:
    """Verify that display and visibility are respectively cascaded, and no element will be output when any calculation result is hidden."""
    stylesheet = MarkupStylesheet()
    stylesheet.add(css)

    resolved = stylesheet.resolve(etree.fromstring(b'<span class="secret"/>'), TextStyle())

    assert resolved.hidden is expected_hidden


def test_epub_combined_visibility_rule_does_not_export_hidden_content() -> None:
    """Verify that the visibility:visible in the real EPUB will not overwrite the display:none of the same rule."""
    source = BytesIO(build_epub_fixture())
    rewritten = BytesIO()
    with ZipFile(source) as archive, ZipFile(rewritten, "w", ZIP_DEFLATED) as output:
        for info in archive.infolist():
            data = archive.read(info.filename)
            if info.filename == "EPUB/styles/book.css":
                data = b".hidden { display: none; visibility: visible; }"
            output.writestr(info, data)

    pages = EpubModel().predict(BytesIO(rewritten.getvalue()))

    assert "hidden secret" not in str(pages)


def test_epub_stylesheet_allows_visible_descendant_to_override_inherited_visibility() -> None:
    """Verify that visibility is inheritable and that explicit visible descendants can restore their own output."""
    stylesheet = MarkupStylesheet()
    stylesheet.add(".parent { visibility: hidden; } .child { visibility: visible; }")
    parent = etree.fromstring(b'<div class="parent"><span class="child"/></div>')
    child = parent[0]

    parent_style = stylesheet.resolve(parent, TextStyle())
    child_style = stylesheet.resolve(child, parent_style.text, parent_style.visibility_hidden)

    assert parent_style.subtree_hidden is False
    assert parent_style.visibility_hidden is True
    assert child_style.subtree_hidden is False
    assert child_style.visibility_hidden is False


def test_epub_visibility_visible_descendants_survive_hidden_containers() -> None:
    """Verify that true EPUB's block, inline, list, table, SVG and title anchors can all be recovered from visibility:hidden."""
    source = BytesIO(build_epub_fixture())
    rewritten = BytesIO()
    replacement = b"""<div class="visibility-parent">
  hidden parent text
  <h2 id="visibility-heading">hidden heading text <span class="visibility-child">Visible descendant heading</span></h2>
  <p>hidden paragraph</p>
  <p class="visibility-child">visible block text</p>
  <p>hidden before <span class="visibility-child">visible inline text</span> hidden after</p>
  <ol><li class="visibility-child">visible list item</li><li>hidden list item</li></ol>
  <table><tbody><tr><td class="visibility-child">visible table cell</td><td>hidden table cell</td></tr></tbody></table>
  <svg xmlns="http://www.w3.org/2000/svg"><g><text class="visibility-child">visible SVG text</text>
    <text>hidden SVG text</text></g></svg>
</div>
<div class="display-parent"><p class="visibility-child">display-hidden descendant</p></div>
<div hidden="hidden"><p class="visibility-child">attribute-hidden descendant</p></div>
<p><a href="#visibility-heading">jump to visible heading</a></p>"""
    with ZipFile(source) as archive, ZipFile(rewritten, "w", ZIP_DEFLATED) as output:
        for info in archive.infolist():
            data = archive.read(info.filename)
            if info.filename == "EPUB/text/ch1.xhtml":
                data = data.replace(b'<p class="hidden">hidden secret</p>', replacement)
            elif info.filename == "EPUB/styles/book.css":
                data = (
                    b".visibility-parent { visibility: hidden; }"
                    b".visibility-child { visibility: visible; }"
                    b".display-parent { display: none; }"
                )
            output.writestr(info, data)

    pages = EpubModel().predict(BytesIO(rewritten.getvalue()))
    blocks = [block for page in pages for block in page]
    flattened = str(blocks)

    for visible_text in (
        "Visible descendant heading",
        "visible block text",
        "visible inline text",
        "visible list item",
        "visible table cell",
        "visible SVG text",
    ):
        assert visible_text in flattened
    for hidden_text in (
        "hidden parent text",
        "hidden heading text",
        "hidden paragraph",
        "hidden before",
        "hidden after",
        "hidden list item",
        "hidden table cell",
        "hidden SVG text",
        "display-hidden descendant",
        "attribute-hidden descendant",
    ):
        assert hidden_text not in flattened

    heading = next(block for block in blocks if inline_text(block.get("content")) == "Visible descendant heading")
    assert f"#{heading['anchor']}" in [url for block in blocks for url in inline_urls(block.get("content"))]


def test_epub_visibility_hidden_body_still_visits_visible_children() -> None:
    """Verify that body itself visibility:hidden does not prune until child nodes are explicitly visible."""
    source = BytesIO(build_epub_fixture())
    rewritten = BytesIO()
    with ZipFile(source) as archive, ZipFile(rewritten, "w", ZIP_DEFLATED) as output:
        for info in archive.infolist():
            data = archive.read(info.filename)
            if info.filename == "EPUB/text/ch1.xhtml":
                data = data.replace(b"<body>", b'<body class="visibility-parent">', 1)
                data = data.replace(b'<h1 id="chapter-one">', b'<h1 id="chapter-one" class="visibility-child">', 1)
            elif info.filename == "EPUB/styles/book.css":
                data = (
                    b".visibility-parent { visibility: hidden; }"
                    b".visibility-child { visibility: visible; }"
                    b".hidden { display: none; }"
                )
            output.writestr(info, data)

    first_page = str(EpubModel().predict(BytesIO(rewritten.getvalue()))[0])

    assert "Chapter One" in first_page
    assert "Hello" not in first_page


def test_epub_stylesheet_rejects_overlong_numeric_font_weight_before_int() -> None:
    """Validation that overlong or out-of-bounds numeric weights are ignored as invalid declarations and will not override inherited styles."""
    stylesheet = MarkupStylesheet()
    stylesheet.add(f".x {{ font-weight: {'9' * 100_000}; }} .y {{ font-weight: 1001; }}")

    inherited = TextStyle(bold=True)
    overlong = stylesheet.resolve(etree.fromstring(b'<span class="x"/>'), inherited)
    out_of_range = stylesheet.resolve(etree.fromstring(b'<span class="y"/>'), inherited)

    assert overlong.text.bold is True
    assert out_of_range.text.bold is True
