"""Verify partial replacement, complex content fallback and native typesetting equivalence for common CJK style comparisons."""

from copy import deepcopy

import pytest
from reportlab.lib import colors
from reportlab.lib.abag import ABag
from reportlab.platypus import Paragraph
from reportlab.platypus import paragraph as rl_paragraph
from reportlab.platypus.paraparser import ParaFrag

from docvortex.render._internal.pdf import paragraph as subject
from docvortex.render._internal.pdf.styles import build_pdf_styles, HAN_FONT


_TEXT = '<font name="Helvetica">Alpha 123 </font>中文混排，标点。<font name="Helvetica"> Beta </font>继续正文'


def _paragraph(cls=subject.PlainCJKParagraph, text=_TEXT):
    """Constructs real CJK fragments of alternating fonts, ensuring path through multi-fragment group lines."""
    style = build_pdf_styles().body.clone("cjk-style-test", fontName=HAN_FONT, wordWrap="CJK")
    return cls(text, style)


def _layout(paragraph):
    """Extract the full group row state and compare geometry and fragment styles with independent deep copies."""

    def normalize(value):
        """Convert ABag without value equality operations into structured data to avoid false reporting of differences based on object identity."""
        if isinstance(value, ABag):
            return normalize(vars(value))
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        return value

    return normalize(paragraph.blPara)


@pytest.mark.parametrize("cls", [subject.PlainCJKParagraph, subject.MeasuredCJKParagraph])
@pytest.mark.parametrize("widths", [(180, 40, 180), (1, 2, 60), (82.5, 82.5, 300)])
def test_plain_cjk_keeps_exact_line_layout(cls, widths):
    """The row geometry and segment order of mixed Chinese and English in the alternating wide and narrow areas and extremely narrow areas remain completely unchanged."""
    actual, expected = _paragraph(cls), _paragraph(Paragraph)
    for width in widths:
        assert actual.wrap(width, 1000) == expected.wrap(width, 1000)
        assert _layout(actual) == _layout(expected)


def test_plain_path_does_not_replace_reportlab_globals(monkeypatch):
    """The local call chain does use fast comparison, while the module entry of the third-party paragraph remains unchanged."""
    original = (rl_paragraph.sameFrag, rl_paragraph.makeCJKParaLine, rl_paragraph.cjkFragSplit, Paragraph.breakLinesCJK)
    local = subject._PLAIN_CJK_BREAK
    calls = []

    def record(self, widths):
        """Count calls made by own paragraphs to local group line entries."""
        calls.append(1)
        return local(self, widths)

    monkeypatch.setattr(subject, "_PLAIN_CJK_BREAK", record)
    _paragraph().wrap(120, 1000)
    _paragraph(Paragraph).wrap(120, 1000)
    assert calls == [1]
    assert original == (rl_paragraph.sameFrag, rl_paragraph.makeCJKParaLine, rl_paragraph.cjkFragSplit, Paragraph.breakLinesCJK)


@pytest.mark.parametrize("change", ["link", "us_lines", "rise", "nobr", "lineBreak", "cbDefn", "split", "bullet", "dots"])
def test_changed_complex_content_returns_to_original(monkeypatch, change):
    """Eligibility is rechecked when rich styles, callbacks, or split tags appear subsequently in ordinary paragraphs."""
    actual = _paragraph()
    actual.wrap(120, 1000)
    if change == "split":
        actual._splitpara = True
    elif change == "bullet":
        actual.bulletText = "bullet"
    elif change == "dots":
        actual.style.endDots = "..."
    else:
        setattr(actual.frags[0], change, True)
    calls = []

    def fallback(self, widths):
        """Intercept the original entry to prevent the fake callback from being actually drawn."""
        calls.append(self)
        return "fallback"

    monkeypatch.setattr(Paragraph, "breakLinesCJK", fallback)
    assert actual.breakLinesCJK([120]) == "fallback"
    assert calls == [actual]


@pytest.mark.parametrize("field", subject._STYLE_FIELDS)
def test_style_comparison_observes_changes(field):
    """Comparison does not cache mutable fields and preserves the native difference between missing attributes and explicit None."""
    first, second = ParaFrag(), ParaFrag()
    assert subject._same_plain_style(first, second)
    setattr(first, field, None)
    assert not subject._same_plain_style(first, second)
    setattr(second, field, None)
    assert subject._same_plain_style(first, second)
    setattr(second, field, "changed")
    assert not subject._same_plain_style(first, second)


def test_mutated_text_and_style_match_fresh_layout():
    """After the text, color and font size of the same paragraph are modified, the old comparison conclusion will not be used again."""
    actual = _paragraph(subject.MeasuredCJKParagraph)
    actual.wrap(120, 1000)
    actual.frags[1].text += "新增文本" * 10
    actual.frags[1].fontSize += 1
    actual.frags[1].textColor = colors.red
    expected = Paragraph("", deepcopy(actual.style), frags=deepcopy(actual.frags))
    assert actual.wrap(120, 1000) == expected.wrap(120, 1000)
    assert _layout(actual) == _layout(expected)


def test_split_paragraphs_keep_independent_layout():
    """After the long paragraph is split, the re-measurement and content of the original paragraph are compared one by one, and the measurement status is not shared."""
    actual = _paragraph(subject.MeasuredCJKParagraph, _TEXT * 20)
    expected = _paragraph(Paragraph, _TEXT * 20)
    actual.wrap(140, 1000)
    expected.wrap(140, 1000)
    pieces, references = actual.split(140, 100), expected.split(140, 100)
    assert len(pieces) == len(references) == 2
    for piece, reference in zip(pieces, references):
        assert piece.wrap(140, 1000) == reference.wrap(140, 1000)
        assert _layout(piece) == _layout(reference)
    assert pieces[0].frags is not pieces[1].frags


def test_unknown_reportlab_implementation_disables_adapter(monkeypatch):
    """The local adapter refuses to guess new call chains after a dependent inner function disappears or is wrapped externally."""
    monkeypatch.delattr(rl_paragraph, "makeCJKParaLine")
    assert subject._plain_cjk_breaker() is None


@pytest.mark.parametrize("text", ["<b>粗体中文</b>", '<a href="https://example.com">链接中文</a>', "第一行<br/>第二行"])
def test_rich_markup_layout_is_unchanged(text):
    """Rich text and forced line breaks are still formatted according to the original line break entry."""
    actual, expected = _paragraph(text=text), _paragraph(Paragraph, text=text)
    assert actual.wrap(70, 1000) == expected.wrap(70, 1000)
    assert _layout(actual) == _layout(expected)


@pytest.mark.parametrize("kind", ["plain", "styled", "link", "code", "title", "anchor", "latin"])
@pytest.mark.parametrize("cached", [False, True])
def test_renderer_selects_only_plain_cjk(kind, cached):
    """The real entry selects ordinary paragraphs semantically, and rich titles, links, styles and codes do not enter the local adapter."""
    from docvortex.render._internal.pdf.formula import FormulaRenderer
    from docvortex.render._internal.pdf.inline import PdfAnchorRegistry, PdfInlineContext, build_pdf_paragraph
    from docvortex.schema import TextSpan

    context = PdfInlineContext(FormulaRenderer(), PdfAnchorRegistry(["target"]), cache_paragraphs=cached)
    styles = build_pdf_styles()
    spans = [TextSpan(type="text", content="中文 mixed 文本" if kind != "latin" else "Latin text")]
    if kind == "styled":
        spans[0].styles = ["bold"]
    if kind == "link":
        from docvortex.schema import HyperlinkSpan

        spans = [
            HyperlinkSpan.model_validate(
                {"type": "hyperlink", "content": [{"type": "text", "content": "链接中文"}], "url": "https://example.com"}
            )
        ]
    result = build_pdf_paragraph(
        spans,
        styles.heading(1) if kind == "title" else styles.body,
        context=context,
        page_idx=0,
        block_index=0,
        block_type="code_body" if kind == "code" else "text",
        max_width=180,
        anchor="target" if kind == "anchor" else None,
    )
    assert isinstance(result, subject.PlainCJKParagraph) is (kind == "plain")
