"""Verify that mixed Chinese and English lines are broken according to Chinese characters to avoid large blank spaces in table notes due to space segmentation."""

from copy import deepcopy
from io import BytesIO

import pytest
from pypdf import PdfReader

from docvortex.document.pdf import PDFDocument
from docvortex.render import PdfLayout, render_pdf
from docvortex.render._internal.pdf.formula import FormulaRenderer
from docvortex.render._internal.pdf.inline import PdfAnchorRegistry, PdfInlineContext, build_pdf_paragraph
from docvortex.render._internal.pdf.styles import build_pdf_styles
from docvortex.schema import HyperlinkSpan, TextSpan
from test_pdf_original_layout import _middle, _text

NOTE = "注:异常值采用 1. 5倍四分位距（1. 5 IQR）原则确定。所有特征数据均分布在1. 5 IQR范围内，未发现异常值，因此数据可直接用于模型训练。"
WIDTH = 232.0554


def _paragraph(spans, *, style=None, block_type="table_footnote", preserve_newlines=False, anchor=None):
    """Parse rich text through production portals, preserving true font metrics and link registrations."""
    return build_pdf_paragraph(
        spans,
        style or build_pdf_styles().footnote,
        context=PdfInlineContext(FormulaRenderer(), PdfAnchorRegistry([anchor] if anchor else [])),
        page_idx=2,
        block_index=12,
        block_type=block_type,
        max_width=WIDTH,
        preserve_newlines=preserve_newlines,
        anchor=anchor,
    )


def _lines(paragraph, width=WIDTH):
    """Read the actual measured line contents instead of just checking the line break mode switch."""
    paragraph.wrap(width, 1000)
    return ["".join(fragment.text for fragment in line.words) for line in paragraph.blPara.lines]


def test_real_chinese_footnote_uses_remaining_line_width():
    """The real table annotation IQR returns to the first line that can be accommodated, and the first two lines no longer leave a large blank space due to Chinese word segmentation."""
    paragraph = _paragraph([TextSpan(type="text", content=NOTE)])
    lines = _lines(paragraph)
    assert "IQR）原则确定" in lines[0]
    assert len(lines) == 3
    assert "".join(lines) == NOTE
    assert all(0 <= line.extraSpace < 8.5 for line in paragraph.blPara.lines[:-1])


def test_font_and_link_boundaries_do_not_move_chinese_phrases():
    """Bold fonts and hyperlink boundaries will not cause the entire Chinese paragraph to move, and link annotations are only generated once."""
    prefix, suffix = NOTE.split("IQR", 1)
    paragraph = _paragraph(
        [
            TextSpan(type="text", content=prefix),
            HyperlinkSpan(type="hyperlink", url="https://example.org/iqr", content=[TextSpan(type="text", content="IQR")]),
            TextSpan(type="text", content=suffix, styles=["bold"]),
        ],
        anchor="note",
    )
    assert "IQR" in _lines(paragraph)[0]
    assert "".join(_lines(paragraph)) == NOTE
    output = BytesIO()
    from reportlab.pdfgen.canvas import Canvas

    canvas = Canvas(output)
    paragraph.drawOn(canvas, 40, 600)
    canvas.save()
    annotations = PdfReader(BytesIO(output.getvalue())).pages[0]["/Annots"]
    assert len(annotations) == 1
    assert annotations[0].get_object()["/A"]["/URI"] == "https://example.org/iqr"


def test_table_text_preserves_explicit_breaks_and_wraps_chinese():
    """Tables retain explicit line breaks, while Chinese can still use the remaining line width at character boundaries."""
    paragraph = _paragraph([TextSpan(type="text", content=NOTE + "\n下一行")], block_type="table_body", preserve_newlines=True)
    lines = _lines(paragraph)
    assert "IQR" in lines[0]
    assert lines[-1] == "下一行"


@pytest.mark.parametrize("kind", ["code_body", "algorithm_body"])
def test_literal_blocks_keep_existing_wrap_mode(kind):
    """Code and algorithm literal blocks containing Chinese do not enable the new natural language line breaking rules."""
    paragraph = _paragraph([TextSpan(type="text", content=NOTE)], block_type=kind, preserve_newlines=True)
    assert paragraph.style.wordWrap is None


def test_cjk_style_is_local_and_english_remains_unchanged():
    """When Chinese and English paragraphs share input styles, they will not contaminate each other, and pure English retains the original space segmentation."""
    style = build_pdf_styles().footnote
    chinese = _paragraph([TextSpan(type="text", content=NOTE)], style=style)
    english = _paragraph([TextSpan(type="text", content="English words use the existing line breaking rules.")], style=style)
    assert chinese.style.wordWrap == "CJK"
    assert english.style is style and style.wordWrap is None


@pytest.mark.parametrize("layout", [PdfLayout.ORIGINAL, PdfLayout.REFLOW])
def test_public_pdf_paths_preserve_all_mixed_text(layout):
    """The two public PDF paths output complete mixed text. The first IQR in the original layout is actually located on the first line and the input remains unchanged."""
    middle = _middle([{"page_idx": 0, "blocks": [_text(NOTE, bbox=(0.1, 0.1, 0.68, 0.25))]}])
    before = deepcopy(middle.to_dict())
    payload = render_pdf(middle, layout=layout)
    with PDFDocument(payload) as document:
        chars = document.get_page_chars_with_geometry(0).chars
        actual = "".join(char["char"] for char in chars)
        assert "".join(actual.split()) == "".join(NOTE.split())
        if layout == PdfLayout.ORIGINAL:
            first_i = next(char for char in chars if char["char"] == "I")
            assert first_i["bbox"][1] == pytest.approx(chars[0]["bbox"][1], abs=3)
    assert middle.to_dict() == before
