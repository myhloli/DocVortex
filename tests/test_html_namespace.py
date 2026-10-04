"""Verify standalone HTML tags, legacy tag fallback, and user payload fidelity."""

from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile

from bs4 import BeautifulSoup
import pytest

from docvortex.analyzers.native import EpubModel, HtmlModel
from docvortex.analyzers.native.html.document import parse_html_document
from docvortex.analyzers.native.html.resources import HtmlResourceContext
from docvortex.codecs import html as codec
from docvortex.render.contracts import RenderMode
from docvortex.render.epub import render_epub
from docvortex.render.html import render_html
from docvortex.render.markdown import render_markdown
from docvortex.schema import MiddleJson, parse_inline_spans


def _document() -> MiddleJson:
    """Create neutral documentation with formulas, footnotes, and algorithms without adding host extensions."""
    return MiddleJson.model_validate(
        {
            "pages": [
                {
                    "page_idx": 0,
                    "blocks": [
                        {
                            "type": "text",
                            "index": 0,
                            "content": [
                                {"type": "text", "content": "Example "},
                                {"type": "equation_inline", "content": "x^2"},
                            ],
                        },
                        {
                            "type": "page_footnote",
                            "index": 1,
                            "anchor": "note",
                            "content": [{"type": "text", "content": "Footnote"}],
                        },
                        {
                            "type": "code",
                            "index": 2,
                            "sub_type": "algorithm",
                            "content": [
                                {
                                    "type": "algorithm_body",
                                    "index": 2,
                                    "content": [{"type": "text", "content": "repeat step"}],
                                }
                            ],
                        },
                    ],
                }
            ],
            "is_full_document": True,
            "metadata": {"file_suffix": "html", "producer": {"name": "docvortex", "version": "0.2.0"}},
            "schema": "docvortex.middle",
            "schema_version": "2.0",
        }
    )


@pytest.mark.parametrize("mode", [RenderMode.DEFAULT, RenderMode.FULL])
@pytest.mark.parametrize("standalone", [False, True])
def test_new_namespace_roundtrip(mode: RenderMode, standalone: bool) -> None:
    """The new HTML independently completes accurate decoding, and styles and scripts do not reference the old prefix."""
    source = _document()
    before = source.to_dict(skip_defaults=False)
    markup = render_html(source, mode=mode, standalone=standalone)
    assert "mineru" not in markup.lower()
    document = parse_html_document(markup.encode())
    decoded = codec.decode_docvortex_html_wire(document.body, HtmlResourceContext(document.source_context))
    assert decoded.fallback_reason is None
    assert decoded.blocks is not None
    assert [block["type"] for block in decoded.blocks] == ["text", "page_footnote", "algorithm"]
    assert parse_inline_spans(decoded.blocks[0]["content"]) == source.pages[0].blocks[0].content
    assert HtmlModel().predict(BytesIO(markup.encode())) == [decoded.blocks]
    assert source.to_dict(skip_defaults=False) == before
    if standalone:
        assert BeautifulSoup(markup, "html.parser").title.string == "DocVortex Document"


@pytest.mark.parametrize("case", ["ordinary", "old", "old_docgale", "missing_version", "unknown_version", "damaged", "empty"])
def test_decode_keeps_absence_invalid_and_empty_distinct(case: str) -> None:
    """Old tags are no longer accurately identified, and illegal new versions maintain different results from legitimate empty content."""
    markup = render_html(_document(), standalone=False)
    if case == "ordinary":
        markup = "<p>ordinary</p>"
    elif case == "old":
        markup = markup.replace("docvortex-", "mineru-")
    elif case == "old_docgale":
        markup = markup.replace("docvortex-", "docgale-")
    elif case == "missing_version":
        markup = markup.replace('data-docvortex-html-version="1"', "")
    elif case == "unknown_version":
        markup = markup.replace('data-docvortex-html-version="1"', 'data-docvortex-html-version="999"')
    elif case == "damaged":
        markup = markup.replace("</article>", "unexpected text</article>")
    else:
        markup = render_html(
            MiddleJson(
                pages=[],
                is_full_document=True,
                metadata={"file_suffix": "html", "producer": {"name": "docvortex", "version": "0.2.0"}},
            ),
            standalone=False,
        )
    document = parse_html_document(markup.encode())
    result = codec.decode_docvortex_html_wire(document.body, HtmlResourceContext(document.source_context))
    if case == "empty":
        assert result.blocks == [] and result.fallback_reason is None
    else:
        assert result.blocks is None
        expected = {"unknown_version": "unsupported_version", "damaged": "non_canonical_wire"}.get(case)
        assert result.fallback_reason == expected
    if case in {"old", "old_docgale"}:
        assert HtmlModel().predict(BytesIO(markup.encode()))[0]


def test_codec_does_not_export_old_aliases() -> None:
    """The old codec name is removed in one go and will not be restored via dynamic compatibility branches."""
    assert codec.DOCVORTEX_HTML_VERSION == "1"
    assert not hasattr(codec, "MINERU_HTML_VERSION")
    assert not hasattr(codec, "decode_mineru_html_wire")


def test_user_text_links_and_code_are_not_namespace_rewritten() -> None:
    """The old brand strings in user text, link targets, and code remain intact."""
    source = _document()
    text = source.pages[0].blocks[0]
    text.content = [
        {"type": "text", "content": 'mineru-document data-mineru-latex="literal" '},
        {"type": "code_inline", "content": 'class="mineru-code"'},
        {
            "type": "hyperlink",
            "url": "https://example.com/mineru-file?q=mineru-text",
            "content": [{"type": "text", "content": "mineru-link"}],
        },
    ]
    before = source.to_dict(skip_defaults=False)
    markup = render_html(source, standalone=False)
    soup = BeautifulSoup(markup, "html.parser")
    assert 'mineru-document data-mineru-latex="literal"' in soup.get_text()
    assert soup.code.get_text() == 'class="mineru-code"'
    assert soup.a["href"] == "https://example.com/mineru-file?q=mineru-text"
    assert soup.article["class"] == ["docvortex-document", "docvortex-document--default"]
    assert source.to_dict(skip_defaults=False) == before


def test_epub_and_markdown_share_new_html_namespace() -> None:
    """The EPUB path uses the same namespace as the XHTML and Markdown inline tags."""
    document = _document()
    payload = render_epub(document)
    with ZipFile(BytesIO(payload)) as archive:
        assert "EPUB/styles/docvortex.css" in archive.namelist()
        assert not any("mineru" in name.lower() for name in archive.namelist())
        for name in archive.namelist():
            if name.endswith((".xhtml", ".css", ".opf")):
                assert b"mineru" not in archive.read(name).lower()
    assert EpubModel().predict(BytesIO(payload))[0]
    markdown = render_markdown(document)
    assert 'class="docvortex-page-footnote"' in markdown
    assert 'class="docvortex-algorithm"' in markdown
    assert "mineru" not in markdown.lower()


def test_explicit_document_title_still_wins() -> None:
    """Explicit titles still take precedence over the default brand title."""
    markup = render_html(_document(), document_title="Custom title")
    assert BeautifulSoup(markup, "html.parser").title.string == "Custom title"
