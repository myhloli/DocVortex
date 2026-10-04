from io import BytesIO
from zipfile import ZipFile

import pytest
from lxml import etree

from _epub_test_utils import build_epub_notes_fixture
from _native_test_utils import analyze_native_test_document
from _span_test_utils import inline_text, inline_urls
from docvortex.analyzers.native.epub.xhtml import _epub_types, _is_individual_note
from docvortex.render import render_epub, render_html, render_markdown
from docvortex.schema import PageFootnoteBlock, TextBlock

EPUB_TYPE = "{http://www.idpf.org/2007/ops}type"


@pytest.mark.parametrize(
    "value",
    [
        "footnote",
        " FOOTNOTE ",
        '"footnote"',
        "'footnote'",
        "“footnote”",
        "‘footnote’",
        " “ footnote ” ",
        "“footnote other”",
        "other “footnote”",
        "“footnote” “other”",
        "'footnote' 'other'",
        "‘endnote’",
        "“rearnote”",
    ],
)
def test_quoted_note_type_tokens(value: str) -> None:
    """Complete token is still recognized semantically as a single comment after removing the paired quotation marks."""
    element = etree.Element("aside", attrib={EPUB_TYPE: value})
    assert _is_individual_note(element)


@pytest.mark.parametrize(
    "value",
    [
        "“footnote",
        "footnote”",
        "'footnote",
        "“footnote’",
        "‘footnote”",
        "“footnotes”",
        "“endnotes”",
        "“noteref”",
        "“unknown”",
        "my-footnote",
        "“footnote-other”",
        "",
        "“”",
    ],
)
def test_invalid_or_collection_note_types_are_not_promoted(value: str) -> None:
    """Unpaired, unknown, and collective markers cannot be mistakenly promoted to single footnotes."""
    element = etree.Element("aside", attrib={EPUB_TYPE: value})
    assert not _is_individual_note(element)


def test_multiple_quoted_tokens_remain_separate() -> None:
    """Multiple individually quoted tokens are not damaged by outer layer peeling."""
    element = etree.Element("aside", attrib={EPUB_TYPE: "“footnote” “other”"})
    assert _epub_types(element) == {"footnote", "other"}


def _quoted_fixture() -> bytes:
    """Only modify the type markup within the composite EPUB, retaining the multi-paragraph, hidden and empty footnote structures."""
    output = BytesIO()
    with ZipFile(BytesIO(build_epub_notes_fixture())) as source, ZipFile(output, "w") as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename.endswith(".xhtml"):
                root = etree.fromstring(data)
                for element in root.iter():
                    value = element.get(EPUB_TYPE)
                    if value in {"footnote", "endnote", "rearnote", "footnotes", "endnotes"}:
                        element.set(EPUB_TYPE, f"“{value}”")
                data = etree.tostring(root)
            target.writestr(info, data)
    return output.getvalue()


def test_quoted_footnotes_preserve_semantics_and_roundtrip_links() -> None:
    """Exception quotes produce the same semantics as standard input, and the three types of output and rereading preserve footnotes and return links."""
    expected, _ = analyze_native_test_document(build_epub_notes_fixture(), file_suffix="epub")
    actual, _ = analyze_native_test_document(_quoted_fixture(), file_suffix="epub")
    assert [page.model_dump() for page in actual.pages] == [page.model_dump() for page in expected.pages]
    notes = [block for page in actual.pages for block in page.blocks if isinstance(block, PageFootnoteBlock)]
    first = next(block for block in notes if inline_text(block.content).startswith("First footnote"))
    assert first.anchor and inline_urls(first.content)
    assert any(block.anchor is None for block in notes)
    assert render_markdown(actual) == render_markdown(expected)
    html = render_html(actual)
    assert html == render_html(expected)
    for payload, suffix in [(html.encode(), "html"), (render_epub(actual), "epub")]:
        restored, _ = analyze_native_test_document(payload, file_suffix=suffix)
        blocks = [block for page in restored.pages for block in page.blocks]
        restored_notes = [block for block in blocks if isinstance(block, PageFootnoteBlock)]
        assert len(restored_notes) == len(notes)
        note = next(block for block in restored_notes if inline_text(block.content).startswith("First footnote"))
        targets = {
            f"#{block.anchor}": block for block in blocks if isinstance(block, (TextBlock, PageFootnoteBlock)) and block.anchor
        }
        assert isinstance(targets[inline_urls(note.content)[0]], TextBlock)


@pytest.mark.parametrize("attribute,value", [("hidden", "hidden"), ("style", "display:none"), ("aria-hidden", "true")])
def test_quoted_hidden_notes_stay_hidden(attribute: str, value: str) -> None:
    """Paired quote tolerance does not change footnote visibility and target materialization constraints."""
    output = BytesIO()
    with ZipFile(BytesIO(_quoted_fixture())) as source, ZipFile(output, "w") as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "EPUB/chapter.xhtml":
                root = etree.fromstring(data)
                next(element for element in root.iter() if element.get("id") == "fn-one").set(attribute, value)
                data = etree.tostring(root)
            target.writestr(info, data)
    actual, _ = analyze_native_test_document(output.getvalue(), file_suffix="epub")
    assert "First footnote paragraph" not in actual.model_dump_json()
