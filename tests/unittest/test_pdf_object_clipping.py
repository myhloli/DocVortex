"""Verify that the clipping chain uses the owning coordinate system, covering nested translations, non-proportional scaling, rotations and empty intersections."""

from io import BytesIO

import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen.canvas import Canvas

from docvortex.document.pdf import PDFDocument


def _nested_clipping_pdf(rotation: int, *, form_rotation: int = 0, inner_offset: float = 10) -> bytes:
    """Construct native vector samples where the background extends beyond the inner Form and the inner layer extends beyond the outer Form."""
    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=(200, 200))
    canvas.beginForm("inner", 0, 0, 50, 40)
    canvas.setFillColorRGB(1, 1, 1)
    canvas.rect(-100, -100, 200, 200, fill=1, stroke=0)
    canvas.setFillColorRGB(1, 0, 0)
    canvas.rect(5, 5, 30, 20, fill=1, stroke=0)
    canvas.endForm()
    canvas.beginForm("outer", 0, 0, 60, 50)
    canvas.translate(inner_offset, 5)
    canvas.scale(2, 1)
    if form_rotation:
        canvas.translate(15, 10)
        canvas.rotate(form_rotation)
        canvas.translate(-15, -10)
    canvas.doForm("inner")
    canvas.endForm()
    canvas.saveState()
    canvas.translate(20, 60)
    canvas.scale(0.5, 0.5)
    canvas.doForm("outer")
    canvas.restoreState()
    canvas.save()
    reader = PdfReader(BytesIO(buffer.getvalue()))
    page = reader.pages[0]
    page.rotate(rotation)
    writer = PdfWriter()
    writer.add_page(page)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize(
    "rotation,expected",
    [
        (0, (25, 117.5, 50, 137.5)),
        (90, (62.5, 25, 82.5, 50)),
        (180, (150, 62.5, 175, 82.5)),
        (270, (117.5, 150, 137.5, 175)),
    ],
)
def test_nested_form_clip_uses_parent_coordinates(rotation: int, expected: tuple) -> None:
    """The same clip cannot be multiplied by internal translations repeatedly; all visible leaves fall within the bounds of the resolved Form."""
    with PDFDocument(_nested_clipping_pdf(rotation)) as pdf:
        forms = pdf.get_page_form_bboxes(0)
        assert len(forms) == 1 and forms[0] == pytest.approx(expected, abs=0.01)
        paths = pdf.get_page_path_infos(0)
        assert len(paths) == 2
        for path in paths:
            assert all(expected[i] <= path.bbox[i] + 0.01 if i < 2 else expected[i] >= path.bbox[i] - 0.01 for i in range(4))


def test_rendered_color_stays_inside_clipped_form() -> None:
    """Verify transformation direction with independently rendered red ink, geometry implementation cannot prove itself."""
    with PDFDocument(_nested_clipping_pdf(0)) as pdf:
        image = pdf.render_page(0, scale=4).pil_image.convert("RGB")
        points = [
            (x / 4, y / 4)
            for y in range(image.height)
            for x in range(image.width)
            if (pixel := image.getpixel((x, y)))[0] > 200 and pixel[1] < 50 and pixel[2] < 50
        ]
    assert points
    assert min(x for x, _ in points) >= 30 and max(x for x, _ in points) < 50
    assert min(y for _, y in points) >= 125 and max(y for _, y in points) < 135


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_rotated_form_matrix_matches_rendered_ink(rotation: int) -> None:
    """Directly rotate the inner Form matrix to compare the red path and rendered ink independently, distinct from page rotation."""
    with PDFDocument(_nested_clipping_pdf(0, form_rotation=rotation)) as pdf:
        paths = [path for path in pdf.get_page_path_infos(0) if path.fill_rgba and path.fill_rgba[:3] == (255, 0, 0)]
        assert len(paths) == 1
        image = pdf.render_page(0, scale=4).pil_image.convert("RGB")
        points = [
            (x / 4, y / 4)
            for y in range(image.height)
            for x in range(image.width)
            if (pixel := image.getpixel((x, y)))[0] > 200 and pixel[1] < 50 and pixel[2] < 50
        ]
        assert points
        ink = (
            min(x for x, _ in points),
            min(y for _, y in points),
            max(x for x, _ in points) + 0.25,
            max(y for _, y in points) + 0.25,
        )
        assert paths[0].bbox == pytest.approx(ink, abs=0.5)


def test_disjoint_parent_clip_keeps_empty_intersection() -> None:
    """Empty clipping intersections cannot degenerate into no clipping, making Form completely invisible and creating giant boxes."""
    with PDFDocument(_nested_clipping_pdf(0, inner_offset=200)) as pdf:
        assert pdf.get_page_path_infos(0) == []
        assert pdf.get_page_form_bboxes(0) == []
