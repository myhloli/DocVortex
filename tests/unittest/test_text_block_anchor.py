from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from zipfile import ZipFile

import pytest
from _native_test_utils import analyze_native_test_document
from bs4 import BeautifulSoup
from lxml import etree
from pydantic import ValidationError
from pypdf import PdfReader

from docvortex.render import render_docx, render_epub, render_html, render_markdown, render_pdf
from docvortex.render._internal.common.planner import build_render_plan
from docvortex.schema import IndexBlock, MiddleJson, PageInfo, Producer, RefTextBlock, TextBlock


def _inline(text: str) -> list[dict[str, str]]:
    """Construct minimal structured text span."""
    return [{"type": "text", "content": text}]


def _middle_with_text_anchor() -> MiddleJson:
    """Constructs a strict document that forward references the top-level TextBlock."""
    return MiddleJson(
        pages=[
            PageInfo(
                page_idx=0,
                blocks=[
                    IndexBlock(
                        type="index",
                        index=0,
                        content=[TextBlock(type="text", anchor="body target", content=_inline("Body target\t3"))],
                    )
                ],
            ),
            PageInfo(
                page_idx=1,
                blocks=[TextBlock(type="text", index=0, anchor="body target", content=_inline("Body paragraph"))],
            ),
        ],
        is_full_document=True,
        metadata={"file_suffix": "docx", "producer": Producer(name="docvortex", version="test")},
        extensions={},
    )


def _zip_text(payload: bytes, name: str) -> str:
    """Read the UTF-8 XML text in the ZIP container."""
    with ZipFile(BytesIO(payload)) as archive:
        return archive.read(name).decode("utf-8")


def test_text_anchor_is_strict_and_blocks_continuation_merge() -> None:
    """Verify that TextBlock accepts anchor while continuations with targets are not absorbed by the planner."""
    anchored = TextBlock(type="text", anchor="target", content=_inline("continued"), continues_prev=True)
    middle = MiddleJson(
        pages=[
            PageInfo(page_idx=0, blocks=[TextBlock(type="text", index=0, content=_inline("first"))]),
            PageInfo(page_idx=1, blocks=[anchored.model_copy(update={"index": 0})]),
        ],
        is_full_document=True,
        metadata={"file_suffix": "docx", "producer": Producer(name="docvortex", version="test")},
        extensions={},
    )

    planned = build_render_plan(middle)
    assert planned[1][0].removed is False
    assert TextBlock.model_validate(anchored.model_dump()).anchor == "target"
    with pytest.raises(ValidationError):
        RefTextBlock.model_validate({"type": "ref_text", "anchor": "target", "content": _inline("ref")})


def test_duplicate_and_empty_text_anchors_emit_only_the_first_visible_target() -> None:
    """Verify that only the first item of duplicate text anchor takes effect, and empty text will not produce dangling targets."""
    middle = MiddleJson(
        pages=[
            PageInfo(
                page_idx=0,
                blocks=[
                    TextBlock(type="text", index=0, anchor="same", content=_inline("First")),
                    TextBlock(type="text", index=1, anchor="same", content=_inline("Second")),
                    TextBlock(type="text", index=2, anchor="empty", content=[]),
                ],
            )
        ],
        is_full_document=True,
        metadata={"file_suffix": "docx", "producer": Producer(name="docvortex", version="test")},
        extensions={},
    )

    assert render_markdown(middle).count('<a id="same"></a>') == 1
    rendered_html = BeautifulSoup(render_html(middle), "html.parser")
    assert len(rendered_html.select('[id="same"]')) == 1
    assert rendered_html.select_one('[id="empty"]') is None


def test_text_anchor_markdown_html_and_html_wire_roundtrip() -> None:
    """Verify Markdown, HTML targets hold TextBlock anchor to and from canonical HTML v1."""
    middle = _middle_with_text_anchor()

    markdown = render_markdown(middle)
    assert "[Body target](#body%20target)" in markdown
    assert '<a id="body target"></a>\nBody paragraph' in markdown

    rendered_html = render_html(middle)
    soup = BeautifulSoup(rendered_html, "html.parser")
    target_wrapper = soup.select_one('.docvortex-block[data-block-type="text"][data-anchor="body target"]')
    assert target_wrapper is not None
    assert target_wrapper.find("p")["id"] == "body-target"
    assert soup.select_one('[data-block-type="index"] a')["href"] == "#body-target"

    decoded, _ = analyze_native_test_document(rendered_html.encode("utf-8"), file_suffix="html")
    decoded_target = next(
        block
        for page in decoded.pages
        for block in page.blocks
        if isinstance(block, TextBlock) and block.anchor == "body target"
    )
    decoded_index = next(block for page in decoded.pages for block in page.blocks if isinstance(block, IndexBlock))
    assert decoded_target.anchor == "body target"
    assert isinstance(decoded_index.content[0], TextBlock)
    assert decoded_index.content[0].anchor == "body target"

    missing = MiddleJson.model_validate(
        {
            **middle.model_dump(mode="python", exclude={"pages"}),
            "pages": [
                {
                    "page_idx": 0,
                    "blocks": [
                        {
                            "type": "index",
                            "index": 0,
                            "content": [{"type": "text", "anchor": "missing", "content": _inline("Missing\t1")}],
                        }
                    ],
                }
            ],
        }
    )
    assert "#missing" not in render_markdown(missing)
    assert BeautifulSoup(render_html(missing), "html.parser").select_one('[data-block-type="index"] a') is None


def test_text_anchor_docx_pdf_and_epub_targets() -> None:
    """Verify that the DOCX bookmark, PDF internal links, and EPUB XHTML targets all point to TextBlock."""
    middle = _middle_with_text_anchor()

    document_xml = _zip_text(render_docx(middle), "word/document.xml")
    assert 'w:bookmarkStart w:id="0" w:name="body_target"' in document_xml
    assert 'w:hyperlink w:anchor="body_target"' in document_xml

    pdf_reader = PdfReader(BytesIO(render_pdf(middle)))
    annotations = [annotation.get_object() for page in pdf_reader.pages for annotation in (page.get("/Annots") or [])]
    assert any("/Dest" in annotation for annotation in annotations)

    epub = render_epub(middle, modified_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    content = etree.fromstring(_zip_text(epub, "EPUB/text/content.xhtml").encode("utf-8"))
    namespaces = {"xhtml": "http://www.w3.org/1999/xhtml"}
    assert content.xpath("//xhtml:p[@id='body-target']/text()", namespaces=namespaces) == ["Body paragraph"]
    navigation = etree.fromstring(_zip_text(epub, "EPUB/nav.xhtml").encode("utf-8"))
    assert "text/content.xhtml#body-target" in navigation.xpath("//xhtml:a/@href", namespaces=namespaces)
