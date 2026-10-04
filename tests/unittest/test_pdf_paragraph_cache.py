"""Verify state invalidation, splitting, and rich text boundaries when measuring common paragraph reuse."""

from copy import deepcopy

import pytest
from reportlab.platypus import Paragraph

from docvortex.render._internal.pdf.paragraph import MeasuredParagraph
from docvortex.render._internal.pdf.styles import build_pdf_styles, HAN_FONT


def test_only_expensive_ordinary_paragraphs_use_measurement_cache():
    """Short Latin Text, original layout, and titles follow native paragraphs to avoid caching costs exceeding the line breaks themselves."""
    from docvortex.render._internal.pdf.formula import FormulaRenderer
    from docvortex.render._internal.pdf.inline import PdfAnchorRegistry, PdfInlineContext, build_pdf_paragraph
    from docvortex.schema import TextSpan

    context = PdfInlineContext(FormulaRenderer(), PdfAnchorRegistry([]), cache_paragraphs=True)
    styles = build_pdf_styles()
    for text, style, enabled, expected in [
        ("short", styles.body, True, False),
        ("long text " * 40, styles.body, True, True),
        ("中文段落", styles.body, True, True),
        ("中文标题", styles.heading(1), True, False),
        ("原布局中文段落", styles.body, False, False),
    ]:
        context.cache_paragraphs = enabled
        paragraph = build_pdf_paragraph(
            [TextSpan(type="text", content=text)],
            style,
            context=context,
            page_idx=0,
            block_index=0,
            block_type="text",
            max_width=200,
        )
        assert isinstance(paragraph, MeasuredParagraph) is expected


def test_same_width_reuses_layout_but_new_width_rewraps(monkeypatch):
    """Available height changes do not affect paragraph wrapping, and width changes still perform native measurements."""
    calls = []
    original = Paragraph.wrap

    def wrap(self, width, height):
        """Log real ReportLab newline call instead of comparing cache internal fields."""
        calls.append((width, height))
        return original(self, width, height)

    monkeypatch.setattr(Paragraph, "wrap", wrap)
    style = build_pdf_styles().body.clone("cached-cjk", fontName=HAN_FONT, wordWrap="CJK")
    paragraph = MeasuredParagraph("中文混排 Alpha Beta " * 15, style)
    expected = paragraph.wrap(180, 1000)
    lines = paragraph.blPara
    assert paragraph.wrap(180, 50) == expected
    assert paragraph.blPara is lines
    assert len(calls) == 1
    paragraph.wrap(80, 1000)
    assert paragraph.wrap(180, 1000) == expected
    assert len(calls) == 3


@pytest.mark.parametrize("change", ["style", "text", "geometry", "leading", "fragment_font"])
def test_changed_state_invalidates_cached_layout(change):
    """Modifications to font, content, geometry, or instance line height are consistent with Paragraph measured from scratch."""
    style = build_pdf_styles().body.clone("mutable-cjk", fontName=HAN_FONT, wordWrap="CJK")
    paragraph = MeasuredParagraph("正文 Mixed text " * 20, style)
    paragraph.wrap(140, 1000)
    before = paragraph.blPara
    if change == "style":
        style.leading += 5
    elif change == "text":
        paragraph.frags[0].text += "追加正文" * 30
    elif change == "geometry":
        paragraph.height += 20
    elif change == "fragment_font":
        paragraph.frags[0].fontSize += 3
    else:
        paragraph.autoLeading = "max"
    reference = Paragraph("", deepcopy(style), frags=deepcopy(paragraph.frags))
    if change == "leading":
        reference.autoLeading = "max"
    assert paragraph.wrap(140, 1000) == reference.wrap(140, 1000)
    assert paragraph.blPara is not before


def test_split_paragraphs_have_independent_layouts():
    """After paging, the two paragraphs wrap independently at different widths, and the original object does not use the cache before splitting."""
    style = build_pdf_styles().body.clone("split-cjk", fontName=HAN_FONT, wordWrap="CJK")
    paragraph = MeasuredParagraph("中英混排 Alpha Beta " * 50, style)
    paragraph.wrap(160, 1000)
    parts = paragraph.split(160, 80)
    assert len(parts) == 2
    assert paragraph._measurement_cache is None
    assert all(isinstance(part, MeasuredParagraph) for part in parts)
    parts[0].wrap(160, 1000)
    first_lines = parts[0].blPara
    parts[1].wrap(90, 1000)
    assert parts[0].blPara is first_lines
    assert parts[0].blPara is not parts[1].blPara


def test_anchor_callbacks_always_use_reportlab_measurement():
    """Paragraphs containing anchor callbacks do not reuse measurements to avoid hiding variable callback states."""
    paragraph = MeasuredParagraph('<a name="target"/>Anchor text', build_pdf_styles().body)
    paragraph.wrap(180, 1000)
    assert paragraph._measurement_cache is None
