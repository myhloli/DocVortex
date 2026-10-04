"""Verify transition, baseline, width recovery of CJK non-text fragments with true PDF output."""

from copy import deepcopy
from io import BytesIO

import pytest
from pypdf import PdfReader
from reportlab.graphics.shapes import Drawing, Rect
from reportlab.lib import colors
from reportlab.platypus import Paragraph
from reportlab.platypus import paragraph as rl_paragraph

from docvortex.render import PdfLayout, render_pdf
from docvortex.render._internal.pdf.diagnostics import collect_pdf_diagnostics
from docvortex.render._internal.pdf.formula import FormulaRenderer, FormulaVector
from docvortex.render._internal.pdf.inline import PdfAnchorRegistry, PdfInlineContext, build_pdf_paragraph
from docvortex.render._internal.pdf.renderer import _PdfCanvas
from docvortex.render._internal.pdf.styles import build_pdf_styles
from docvortex.schema import EquationInlineSpan, TextSpan
from test_pdf_original_layout import _middle, _text


class _SizedFormulas(FormulaRenderer):
    """Replace font metrics with explicit width and height vectors so that border and baseline testing does not rely on the ZiaMath glyph."""

    def render(self, latex: str, *, inline: bool, font_size: float, color: str = "#1f2937") -> FormulaVector:
        """Formula proxies are constructed that contain actual ink, and draw tests can detect silent loss."""
        width = float(latex)
        drawing = Drawing(width, 20)
        drawing.add(Rect(0, 0, width, 20, fillColor=colors.red, strokeColor=None))
        return FormulaVector(drawing, width, 20, 15, 5)


def _paragraph(parts: list[str | float], *, cached: bool = False, anchor: str | None = None, indent: float = 0):
    """Generate Chinese text, formulas, anchor points and explicit line breaks by producing paragraph construction entries."""
    spans = [
        TextSpan(type="text", content=part)
        if isinstance(part, str)
        else EquationInlineSpan(type="equation_inline", content=str(part))
        for part in parts
    ]
    context = PdfInlineContext(_SizedFormulas(), PdfAnchorRegistry([anchor] if anchor else []), cache_paragraphs=cached)
    return build_pdf_paragraph(
        spans,
        build_pdf_styles().body.clone("object-test", fontSize=10, leading=12, firstLineIndent=indent),
        context=context,
        page_idx=2,
        block_index=7,
        block_type="text",
        max_width=float("inf"),
        preserve_newlines=True,
        anchor=anchor,
    )


def _images(paragraph):
    """Extract the formula callbacks that remain in the final group of rows, rather than the original input fragment."""
    return [
        f.cbDefn
        for line in paragraph.blPara.lines
        for f in line.words
        if getattr(getattr(f, "cbDefn", None), "kind", None) == "img"
    ]


def _text_content(paragraph) -> str:
    """Check the text order in the group row results and whether spaces have been inserted into pages."""
    return "".join(f.text for line in paragraph.blPara.lines for f in line.words)


def _assert_bounds(paragraph) -> None:
    """Recalculate the true object width of each row independently, instead of just trusting the box width returned by Paragraph."""
    for line in paragraph.blPara.lines:
        actual = sum(
            getattr(fragment.cbDefn, "width", 0)
            if hasattr(fragment, "cbDefn")
            else rl_paragraph.stringWidth(fragment.text, fragment.fontName, fragment.fontSize)
            for fragment in line.words
        )
        assert actual <= line.maxWidth + 1e-7
        assert line.currentWidth == pytest.approx(actual)


@pytest.mark.parametrize("cached", [False, True])
def test_formula_moves_intact_to_next_line(cached):
    """When the formula does not fit into the remaining space, it will move as a whole and cannot be used as a hanging punctuation point or shrunk into the remaining space."""
    paragraph = _paragraph(["中文前", 80.0, "后文"], cached=cached)
    paragraph.wrap(100, 1000)
    assert not any(hasattr(f, "cbDefn") for f in paragraph.blPara.lines[0].words)
    assert [image.width for image in _images(paragraph)] == [80]
    assert _text_content(paragraph) == "中文前后文"
    _assert_bounds(paragraph)


def test_latin_backtracking_crosses_formula_without_ord_error():
    """Latin overflow and traceback will pass through the empty text formula, covering the second ord call."""
    paragraph = _paragraph(["中AAAAAAAA", 20.0, "aaaaaaa"])
    paragraph.wrap(100, 1000)
    assert len(_images(paragraph)) == 1
    assert _text_content(paragraph) == "中AAAAAAAAaaaaaaa"
    _assert_bounds(paragraph)


@pytest.mark.parametrize("cached", [False, True])
def test_oversize_formula_fits_empty_line_and_restores_on_rewrap(cached):
    """Only temporary callbacks are made for scaling. After the width and width alternate, the width, height and baseline are restored from the baseline, and the original minimum column width remains unchanged."""
    paragraph = _paragraph([300.0, "中文"], cached=cached)
    original = next(f.cbDefn for f in paragraph.frags if hasattr(f, "cbDefn"))
    natural_minimum = paragraph.minWidth()
    for width in [100, 40, 400]:
        paragraph.wrap(width, 1000)
        image = _images(paragraph)[0]
        ratio = min(width / 300, 1)
        assert (image.width, image.height, image.valign) == pytest.approx((300 * ratio, 20 * ratio, -5 * ratio))
        assert paragraph.blPara.lines[0].ascent == pytest.approx(15 * ratio)
        _assert_bounds(paragraph)
    assert (original.width, original.height, original.valign) == (300, 20, -5)
    assert paragraph.minWidth() == natural_minimum


def test_first_line_indent_adjacent_formulas_and_explicit_breaks():
    """When first-line indentation, consecutive formulas, and explicit line breaks work together, all objects appear only once."""
    paragraph = _paragraph([120.0, 60.0, "中文\n结束"], anchor="target", indent=30)
    paragraph.wrap(100, 1000)
    assert [image.width for image in _images(paragraph)] == [70, 60]
    assert _text_content(paragraph) == "中文结束"
    assert paragraph.blPara.lines[-1].words[-1].text == "结束"
    _assert_bounds(paragraph)


def test_zero_width_anchor_survives_after_forced_break():
    """The end anchor with a width of zero still needs to be drawn and cannot be discarded by the cumulative width judgment."""
    from docvortex.render._internal.pdf.paragraph import CJKParagraph

    style = build_pdf_styles().body.clone("anchor-test", wordWrap="CJK")
    paragraph = CJKParagraph('中文<br/><a name="end"/>', style)
    paragraph.wrap(100, 1000)
    output = BytesIO()
    canvas = _PdfCanvas(output, document_title="anchors")
    paragraph.drawOn(canvas, 20, 100)
    assert canvas._destinations["end"].fmt is not None
    canvas.save()


def test_page_split_preserves_objects_text_and_natural_geometry():
    """Pagination does not modify the parent paragraph, does not fill in spaces, and restores the original formula size after the continuation paragraph is widened again."""
    paragraph = _paragraph(["中文", 180.0, "后文\n"] * 12, cached=True)
    paragraph.wrap(100, 1000)
    original_text = _text_content(paragraph)
    pieces = paragraph.split(100, 90)
    assert len(pieces) == 2
    assert _text_content(paragraph) == original_text
    for piece in pieces:
        piece.wrap(100, 1000)
        _assert_bounds(piece)
    assert "".join(_text_content(piece) for piece in pieces) == original_text
    assert sum(len(_images(piece)) for piece in pieces) == 12
    pieces[1].wrap(300, 1000)
    assert all(image.width == 180 for image in _images(pieces[1]))


def test_small_formula_diagnostic_only_describes_final_drawing():
    """Trial rendering does not report low font sizes, and final drawing diagnoses are performed with actual page block positioning."""
    paragraph = _paragraph([300.0, "中文"])
    with collect_pdf_diagnostics() as diagnostics:
        paragraph.wrap(30, 1000)
        assert not diagnostics
        canvas = _PdfCanvas(BytesIO(), document_title="diagnostics")
        paragraph.drawOn(canvas, 20, 100)
        canvas.save()
    assert len(diagnostics) == 1
    assert diagnostics[0].code == "pdf_layout_small_text"
    assert diagnostics[0].page_index == 2
    assert "block_index=7" in diagnostics[0].message


@pytest.mark.parametrize("layout", [PdfLayout.AUTO, PdfLayout.ORIGINAL, PdfLayout.REFLOW])
def test_public_render_preserves_vectors_links_and_input(layout):
    """The real long formula goes through the public rendering portal, outputting vectors with links and leaving MiddleJson unchanged."""
    block = _text("中文前文", bbox=(0.1, 0.1, 0.4, 0.8))
    block["content"] += [
        {"type": "equation_inline", "content": "+".join(["x_i"] * 80)},
        {"type": "hyperlink", "url": "https://example.com", "content": [{"type": "text", "content": "中文后文"}]},
    ]
    middle = _middle([{"page_idx": 0, "blocks": [block]}])
    before = deepcopy(middle.to_dict())
    payload = render_pdf(middle, layout=layout)
    reader = PdfReader(BytesIO(payload))
    assert "中文前文" in reader.pages[0].extract_text()
    assert "中文后文" in reader.pages[0].extract_text()
    assert reader.pages[0]["/Annots"][0].get_object()["/A"]["/URI"] == "https://example.com"
    assert b" c" in reader.pages[0].get_contents().get_data()
    assert middle.to_dict() == before


def test_safe_path_does_not_change_reportlab_globals():
    """Safe paths do not modify third-party line breaks, character objects, or Paragraph entries."""
    original = (rl_paragraph.cjkFragSplit, rl_paragraph.cjkU, Paragraph.breakLinesCJK)
    _paragraph(["中文", 300.0]).wrap(100, 1000)
    assert original == (rl_paragraph.cjkFragSplit, rl_paragraph.cjkU, Paragraph.breakLinesCJK)


def test_table_cell_keeps_natural_width_and_draws_fitted_formula():
    """Table column width measurements retain the formula's natural width, and actual narrow column drawing can still be scaled without contaminating subsequent wide columns."""
    from reportlab.platypus import Table

    paragraph = _paragraph(["中文", 180.0, "后文"], cached=True)
    minimum = paragraph.minWidth()
    for width in (100, 300):
        canvas = _PdfCanvas(BytesIO(), document_title="table")
        table = Table([[paragraph]], colWidths=[width + 12])
        table.wrapOn(canvas, width + 12, 1000)
        table.drawOn(canvas, 20, 100)
        canvas.save()
        assert _images(paragraph)[0].width == min(180, width)
        assert paragraph.minWidth() == minimum
        _assert_bounds(paragraph)


def test_mutating_special_paragraph_back_to_plain_clears_safe_state():
    """After a variable paragraph reverts to a single plain text, Drawing can no longer treat normal line structures as rich fragments."""
    paragraph = _paragraph(["中文", 180.0])
    paragraph.wrap(100, 1000)
    paragraph.frags = [paragraph.frags[0]]
    paragraph.wrap(100, 1000)
    canvas = _PdfCanvas(BytesIO(), document_title="mutated")
    paragraph.drawOn(canvas, 20, 100)
    canvas.save()
    assert not paragraph._pdf_safe_cjk
