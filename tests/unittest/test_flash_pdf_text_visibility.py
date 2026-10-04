"""Verify the compatibility boundaries of the Flash visible character view with the original text API."""

from io import BytesIO

import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen.canvas import Canvas

from docvortex.document.pdf import PDFDocument


def _visibility_pdf(rotation: int = 0, form_rotation: int = 0) -> bytes:
    """Construct hidden, transparent, white text and nested Form fully and partially cropped text."""
    stream = BytesIO()
    painter = Canvas(stream, pagesize=(300, 220))
    painter.setFont("Helvetica", 10)
    painter.drawString(20, 195, "VISIBLE")
    for mode, text, y in [(3, "HIDDEN_LAYER", 170), (7, "CLIP_ONLY", 150)]:
        painter.saveState()
        run = painter.beginText(20, y)
        run.setFont("Helvetica", 10)
        run.setTextRenderMode(mode)
        run.textOut(text)
        painter.drawText(run)
        painter.restoreState()
    painter.saveState()
    painter.setFillAlpha(0)
    painter.drawString(20, 130, "TRANSPARENT")
    painter.restoreState()
    painter.setFillColorRGB(0, 0, 1)
    painter.rect(20, 95, 100, 20, fill=1, stroke=0)
    painter.setFillColorRGB(1, 1, 1)
    painter.setFont("Helvetica", 3)
    painter.drawString(25, 100, "WHITE_SMALL")
    painter.setFillColorRGB(0, 0, 0)
    painter.beginForm("inner", 0, 0, 150, 40)
    painter.setFont("Helvetica", 10)
    painter.drawString(0, 10, "INSIDE")
    painter.drawString(50, 10, "M")
    painter.drawString(90, 10, "OUTSIDE")
    painter.endForm()
    painter.beginForm("outer", 0, 0, 65, 40)
    painter.translate(12, 0)
    painter.doForm("inner")
    painter.endForm()
    painter.saveState()
    painter.translate(20, 30)
    painter.rotate(form_rotation)
    painter.doForm("outer")
    painter.restoreState()
    painter.save()
    reader = PdfReader(BytesIO(stream.getvalue()))
    writer = PdfWriter()
    page = reader.pages[0]
    page.rotate(rotation)
    writer.add_page(page)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("form_rotation", [0, 90])
def test_flash_visibility_filters_only_unpainted_or_fully_clipped_characters(rotation: int, form_rotation: int) -> None:
    """Only undrawn and fully cropped text are excluded in the four page orientations, and the original API still retains the only hidden text layer."""
    with PDFDocument(_visibility_pdf(rotation, form_rotation)) as pdf:
        raw = pdf.get_page_chars_with_geometry(0)
        visible = pdf._extract_native_page(0).text_geometry
    raw_text = "".join(char["char"] for char in raw.chars)
    visible_text = "".join(char["char"] for char in visible.chars)
    for text in ["HIDDEN_LAYER", "CLIP_ONLY", "TRANSPARENT", "OUTSIDE"]:
        assert text in raw_text
        assert text not in visible_text
    for text in ["VISIBLE", "WHITE_SMALL", "INSIDE", "M"]:
        assert text in visible_text
    originals = {char["char_idx"]: char for char in raw.chars}
    assert all(char["char"] == originals[char["char_idx"]]["char"] for char in visible.chars)
    assert all(char["origin"] == originals[char["char_idx"]]["origin"] for char in visible.chars)
    partially_visible = [char for char in visible.chars if char["char"] == "M"][-1]
    original = originals[partially_visible["char_idx"]]
    axis = 1 if (rotation + form_rotation) % 180 else 0
    assert (
        partially_visible["bbox"][axis + 2] - partially_visible["bbox"][axis]
        < original["bbox"][axis + 2] - original["bbox"][axis]
    )
    assert partially_visible["source_indices"] == original["source_indices"]


def test_fully_hidden_layer_does_not_create_flash_text_blocks() -> None:
    """Only the blank page hiding the OCR text does not produce the Flash text, and the original extraction contract remains unchanged."""
    from docvortex.analyzers.native import PdfModel

    stream = BytesIO()
    painter = Canvas(stream, pagesize=(200, 200))
    run = painter.beginText(20, 100)
    run.setTextRenderMode(3)
    run.textOut("Only invisible OCR text")
    painter.drawText(run)
    painter.save()
    with PDFDocument(stream.getvalue()) as pdf:
        assert "invisible" in "".join(char["char"] for char in pdf.get_page_chars(0))
        model = PdfModel().predict(pdf)
        assert not any(block["type"] in {"text", "paragraph_title", "equation", "table"} for block in model[0])
