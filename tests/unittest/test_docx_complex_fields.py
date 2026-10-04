from __future__ import annotations

from io import BytesIO

from _native_test_utils import analyze_native_test_document
from _span_test_utils import inline_urls
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from docvortex.analyzers.native import DocxModel
from docvortex.analyzers.native.office.docx.docx_converter import DocxConverter
from docvortex.content.inline import inline_plain_text
from docvortex.content.spans import inline_span_plain_text
from docvortex.schema import IndexBlock, ParagraphTitleBlock, TextBlock


def _append_field_char(paragraph: Paragraph, field_type: str) -> None:
    """Appends a complex field boundary run to the paragraph."""
    run = OxmlElement("w:r")
    field_char = OxmlElement("w:fldChar")
    field_char.set(qn("w:fldCharType"), field_type)
    run.append(field_char)
    paragraph._p.append(run)


def _append_instruction(paragraph: Paragraph, instruction: str) -> None:
    """Append complex field command run to paragraph."""
    run = OxmlElement("w:r")
    instruction_element = OxmlElement("w:instrText")
    instruction_element.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    instruction_element.text = instruction
    run.append(instruction_element)
    paragraph._p.append(run)


def _append_result_text(
    paragraph: Paragraph,
    text: str,
    *,
    bold: bool = False,
) -> None:
    """Appends a visible text run to the field results."""
    run = OxmlElement("w:r")
    if bold:
        run_properties = OxmlElement("w:rPr")
        run_properties.append(OxmlElement("w:b"))
        run.append(run_properties)
    text_element = OxmlElement("w:t")
    if text != text.strip():
        text_element.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    text_element.text = text
    run.append(text_element)
    paragraph._p.append(run)


def _append_native_hyperlink(
    paragraph: Paragraph,
    target: str,
    parts: list[tuple[str, bool]],
) -> None:
    """Appends a real external hyperlink consisting of multiple formats run to the paragraph."""

    relationship_id = paragraph.part.relate_to(
        target,
        RELATIONSHIP_TYPE.HYPERLINK,
        is_external=True,
    )
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), relationship_id)
    for text, bold in parts:
        run = OxmlElement("w:r")
        if bold:
            run_properties = OxmlElement("w:rPr")
            run_properties.append(OxmlElement("w:b"))
            run.append(run_properties)
        text_element = OxmlElement("w:t")
        if text != text.strip():
            text_element.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        text_element.text = text
        run.append(text_element)
        hyperlink.append(run)
    paragraph._p.append(hyperlink)


def _append_result_tab(paragraph: Paragraph) -> None:
    """Appends the Word tab character run to the field results."""
    run = OxmlElement("w:r")
    run.append(OxmlElement("w:tab"))
    paragraph._p.append(run)


def _append_toc_complex_field(
    paragraph: Paragraph,
    *,
    title: str,
    page_number: str,
    anchor: str,
    include_outer_toc: bool,
    split_hyperlink_instruction: bool = False,
) -> None:
    """Construct WPS common TOC, HYPERLINK and PAGEREF nested complex domains."""
    if include_outer_toc:
        _append_field_char(paragraph, "begin")
        _append_instruction(paragraph, ' TOC \\o "1-1" \\h \\z \\u ')
        _append_field_char(paragraph, "separate")

    _append_field_char(paragraph, "begin")
    if split_hyperlink_instruction:
        split_at = max(1, len(anchor) // 2)
        _append_instruction(paragraph, " HYPER")
        _append_instruction(paragraph, f'LINK \\l "{anchor[:split_at]}')
        _append_instruction(paragraph, f'{anchor[split_at:]}" ')
    else:
        _append_instruction(paragraph, f' HYPERLINK \\l "{anchor}" ')
    _append_field_char(paragraph, "separate")
    _append_result_text(paragraph, title)
    _append_result_tab(paragraph)
    _append_field_char(paragraph, "begin")
    _append_instruction(paragraph, f" PAGEREF {anchor} \\h ")
    _append_field_char(paragraph, "separate")
    _append_result_text(paragraph, page_number)
    _append_field_char(paragraph, "end")
    _append_field_char(paragraph, "end")

    if include_outer_toc:
        _append_field_char(paragraph, "end")


def _attach_bookmark(paragraph: Paragraph, anchor: str, bookmark_id: int) -> None:
    """Surround the existing text of the paragraph with bookmark to simulate the real jump target of TOC."""
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), str(bookmark_id))
    start.set(qn("w:name"), anchor)
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), str(bookmark_id))
    insert_at = 1 if paragraph._p.pPr is not None else 0
    paragraph._p.insert(insert_at, start)
    paragraph._p.append(end)


def _build_complex_toc_docx() -> bytes:
    """Generate a minimally complex domain directory DOCX containing matching and missing targets."""
    document = Document()
    toc_style = document.styles.add_style("TOC 1", WD_STYLE_TYPE.PARAGRAPH)

    first = document.add_paragraph(style=toc_style)
    _append_toc_complex_field(
        first,
        title="一、建设项目基本情况",
        page_number="1",
        anchor="_TocTarget",
        include_outer_toc=True,
        split_hyperlink_instruction=True,
    )
    second = document.add_paragraph(style=toc_style)
    _append_toc_complex_field(
        second,
        title="二、缺失章节",
        page_number="9",
        anchor="_TocMissing",
        include_outer_toc=False,
    )

    heading = document.add_heading("一、建设项目基本情况", level=1)
    _attach_bookmark(heading, "_TocTarget", 7)

    output = BytesIO()
    document.save(output)
    return output.getvalue()


def _build_external_bookmark_field_docx() -> bytes:
    """Generates a complex field that contains both the external document address and bookmark switch."""

    document = Document()
    paragraph = document.add_paragraph()
    _append_field_char(paragraph, "begin")
    _append_instruction(
        paragraph,
        ' HYPERLINK "chapter.docx" \\l "Section1" ',
    )
    _append_field_char(paragraph, "separate")
    _append_result_text(paragraph, "Open ")
    _append_result_text(paragraph, "section", bold=True)
    _append_field_char(paragraph, "end")
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def _build_multi_alias_toc_docx() -> bytes:
    """Generates two references TOC bookmark pointing to the same body paragraph DOCX."""

    document = Document()
    toc_style = document.styles.add_style("TOC 1", WD_STYLE_TYPE.PARAGRAPH)
    for title, anchor in (
        ("Primary entry", "_TocPrimary"),
        ("Alias entry", "_TocAlias"),
    ):
        paragraph = document.add_paragraph(style=toc_style)
        _append_toc_complex_field(
            paragraph,
            title=title,
            page_number="1",
            anchor=anchor,
            include_outer_toc=False,
            split_hyperlink_instruction=True,
        )

    heading = document.add_heading("Shared target", level=1)
    insert_at = 1 if heading._p.pPr is not None else 0
    for offset, (bookmark_id, anchor) in enumerate(
        ((10, "_TocPrimary"), (11, "_TocAlias")),
    ):
        start = OxmlElement("w:bookmarkStart")
        start.set(qn("w:id"), str(bookmark_id))
        start.set(qn("w:name"), anchor)
        heading._p.insert(insert_at + offset, start)
        end = OxmlElement("w:bookmarkEnd")
        end.set(qn("w:id"), str(bookmark_id))
        heading._p.append(end)

    output = BytesIO()
    document.save(output)
    return output.getvalue()


def test_nested_complex_toc_fields_preserve_titles_and_strict_index_targets() -> None:
    """Verify that split and nested complex fields are not reduced to page numbers and converge the table of contents by true text target anchor."""
    file_bytes = _build_complex_toc_docx()
    model_pages = DocxModel().predict(BytesIO(file_bytes))
    raw_index = next(block for page in model_pages for block in page if block["type"] == "index")

    assert [inline_span_plain_text(child["content"]) for child in raw_index["content"]] == [
        "一、建设项目基本情况\t1",
        "二、缺失章节\t9",
    ]
    assert [child.get("anchor") for child in raw_index["content"]] == ["_TocTarget", "_TocMissing"]

    middle_json, _ = analyze_native_test_document(file_bytes, file_suffix="docx")
    typed_index = next(block for page in middle_json.pages for block in page.blocks if isinstance(block, IndexBlock))
    assert isinstance(typed_index.content[0], ParagraphTitleBlock)
    assert typed_index.content[0].anchor == "_TocTarget"
    assert inline_plain_text(typed_index.content[0].content) == "一、建设项目基本情况\t1"
    assert isinstance(typed_index.content[1], TextBlock)
    assert typed_index.content[1].anchor is None
    assert inline_plain_text(typed_index.content[1].content) == "二、缺失章节\t9"


def test_external_complex_hyperlink_preserves_bookmark_switch() -> None:
    """Verify that the external complex field combines the document address and bookmark into the same link target."""

    file_bytes = _build_external_bookmark_field_docx()
    model_pages = DocxModel().predict(BytesIO(file_bytes))
    raw_text = next(block for page in model_pages for block in page if block["type"] == "text")

    assert inline_span_plain_text(raw_text["content"]) == "Open section"
    assert inline_urls(raw_text["content"]) == ["chapter.docx#Section1"]
    assert [child["styles"] for child in raw_text["content"][0]["content"]] == [[], ["bold"]]

    middle_json, _ = analyze_native_test_document(file_bytes, file_suffix="docx")
    typed_text = next(block for page in middle_json.pages for block in page.blocks if isinstance(block, TextBlock))
    assert inline_plain_text(typed_text.content) == "Open section"
    assert inline_urls(typed_text.content) == ["chapter.docx#Section1"]


def test_split_toc_bookmark_aliases_collapse_to_one_target_anchor() -> None:
    """Verify that the split field referenced by the same paragraph as bookmark aliases converges to the only public anchor."""

    file_bytes = _build_multi_alias_toc_docx()
    model_pages = DocxModel().predict(BytesIO(file_bytes))
    raw_index = next(block for page in model_pages for block in page if block["type"] == "index")
    raw_heading = next(
        block
        for page in model_pages
        for block in page
        if block["type"] == "paragraph_title" and inline_span_plain_text(block["content"]) == "Shared target"
    )

    assert raw_heading["anchor"] == "_TocPrimary"
    assert [child.get("anchor") for child in raw_index["content"]] == [
        "_TocPrimary",
        "_TocPrimary",
    ]

    middle_json, _ = analyze_native_test_document(file_bytes, file_suffix="docx")
    typed_index = next(block for page in middle_json.pages for block in page.blocks if isinstance(block, IndexBlock))
    typed_heading = next(
        block
        for page in middle_json.pages
        for block in page.blocks
        if isinstance(block, ParagraphTitleBlock) and inline_plain_text(block.content) == "Shared target"
    )
    assert typed_heading.anchor == "_TocPrimary"
    assert [child.anchor for child in typed_index.content] == [
        "_TocPrimary",
        "_TocPrimary",
    ]


def test_native_hyperlink_preserves_spaces_between_formatted_runs() -> None:
    """Preserve internal whitespace and maintain a single link target when validating real hyperlinks across formats run."""

    document = Document()
    paragraph = document.add_paragraph()
    _append_native_hyperlink(
        paragraph,
        "https://example.test/section",
        [("Open ", False), ("section", True)],
    )
    output = BytesIO()
    document.save(output)

    model_pages = DocxModel().predict(BytesIO(output.getvalue()))
    raw_text = next(block for page in model_pages for block in page if block["type"] == "text")

    assert inline_span_plain_text(raw_text["content"]) == "Open section"
    assert inline_urls(raw_text["content"]) == ["https://example.test/section"]
    assert [child["styles"] for child in raw_text["content"][0]["content"]] == [[], ["bold"]]


def test_native_hyperlink_moves_trailing_space_to_plain_text() -> None:
    """The trailing space of the verification link is moved to the subsequent normal text, which neither retains the separation nor expands the clickable range."""

    document = Document()
    paragraph = document.add_paragraph()
    _append_native_hyperlink(
        paragraph,
        "https://example.test/link",
        [("link ", False)],
    )
    paragraph.add_run("after")
    output = BytesIO()
    document.save(output)

    model_pages = DocxModel().predict(BytesIO(output.getvalue()))
    raw_text = next(block for page in model_pages for block in page if block["type"] == "text")

    assert inline_span_plain_text(raw_text["content"]) == "link after"
    assert inline_urls(raw_text["content"]) == ["https://example.test/link"]
    assert inline_span_plain_text(raw_text["content"][0]["content"]) == "link"
    assert raw_text["content"][1] == {
        "type": "text",
        "content": " after",
    }


def test_hyperlink_boundary_spaces_are_plain_and_only_paragraph_edges_trim() -> None:
    """Verify that the spaces at the beginning and end of the link are moved to ordinary elements, the space between different links is retained, and only the outer edge of the paragraph is cropped."""

    first_target = "https://example.test/first"
    second_target = "https://example.test/second"

    assert DocxConverter._normalize_hyperlink_group_boundaries(
        [
            ("before", None, None),
            (" link ", None, first_target),
            ("after", None, None),
        ]
    ) == [
        ("before", None, None),
        (" ", None, None),
        ("link", None, first_target),
        (" ", None, None),
        ("after", None, None),
    ]
    assert DocxConverter._normalize_hyperlink_group_boundaries(
        [
            ("first ", None, first_target),
            ("second", None, second_target),
        ]
    ) == [
        ("first", None, first_target),
        (" ", None, None),
        ("second", None, second_target),
    ]
    assert DocxConverter._normalize_hyperlink_group_boundaries([(" link ", None, first_target)]) == [
        ("link", None, first_target)
    ]
