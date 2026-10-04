"""Verify the semantics, page mapping and actual display coordinates of the shared PDF layout annotation."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np
import pypdfium2 as pdfium
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link
from pypdf.generic import RectangleObject
from reportlab.pdfgen import canvas

from docvortex.document.pdf._document import PDFDocument
from docvortex.schema import PageInfo
from docvortex.visualization import render_layout_pdf


def _source_pdf(
    page_count: int = 1,
    *,
    rotation: int = 0,
    media: tuple[int, int, int, int] = (0, 0, 300, 400),
    crop: tuple[int, int, int, int] = (0, 0, 200, 300),
) -> bytes:
    """Generate PDF with original text and links, and set the page frame and rotation mark independently."""
    buffer = BytesIO()
    painter = canvas.Canvas(buffer, pagesize=(300, 400))
    for index in range(page_count):
        painter.drawString(50, 100, f"Original page {index}")
        painter.showPage()
    painter.save()
    writer = PdfWriter()
    for page in PdfReader(BytesIO(buffer.getvalue())).pages:
        page.mediabox = RectangleObject(media)
        page.cropbox = RectangleObject(crop)
        page.rotate(rotation)
        writer.add_page(page)
    writer.add_annotation(0, Link(rect=(50, 90, 120, 110), url="https://example.com/"))
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _text_block(index: int = 0, **updates: Any) -> dict[str, Any]:
    """Create text blocks with fixed normalization bbox, allowing fields to be overridden for specific semantics."""
    return {
        "type": "text",
        "index": index,
        "bbox": (0.1, 0.2, 0.6, 0.4),
        "content": [{"type": "text", "content": "布局文字"}],
        **updates,
    }


def _page(page_idx: int = 0, blocks: list[dict[str, Any]] | None = None) -> PageInfo:
    """Prevent the use of old fields from bypassing the real contract through the official schema verification test page."""
    return PageInfo.model_validate({"page_idx": page_idx, "blocks": [_text_block()] if blocks is None else blocks})


def _outlines(data: bytes, page_idx: int = 0) -> list[tuple[tuple[float, ...], tuple[float, ...]]]:
    """Extract the stroked rectangle and color from the actual output content stream, ignoring the PDF merged cropping rectangle."""
    page = PdfReader(BytesIO(data)).pages[page_idx]
    content = page.get_contents()
    result = []
    color: tuple[float, ...] = ()
    rectangle: tuple[float, ...] = ()
    if content is not None:
        for operands, operator in content.operations:
            if operator == b"RG":
                color = tuple(float(value) for value in operands)
            elif operator == b"re":
                rectangle = tuple(float(value) for value in operands)
            elif operator == b"S" and rectangle:
                result.append((color, rectangle))
                rectangle = ()
            elif operator == b"n":
                rectangle = ()
    return result


def _label_backgrounds(data: bytes) -> list[tuple[float, ...]]:
    """Read the label base and confirm that only the small white rectangle is filled."""
    operations = PdfReader(BytesIO(data)).pages[0].get_contents().operations
    rectangles = []
    color = ()
    rectangle = ()
    for operands, operator in operations:
        if operator == b"rg":
            color = tuple(float(value) for value in operands)
        elif operator == b"re":
            rectangle = tuple(float(value) for value in operands)
        elif operator in {b"f", b"f*", b"B", b"B*"}:
            assert color == (1, 1, 1)
            assert operator in {b"f", b"f*"}
            rectangles.append(rectangle)
    return rectangles


def _render_pixels(data: bytes) -> np.ndarray:
    """Actually render the first page and copy the RGB pixels, making sure it's still comparable after closing the PDFium resource."""
    with pdfium.PdfDocument(data) as document:
        page = document[0]
        bitmap = page.render(scale=2)
        try:
            return np.asarray(bitmap.to_pil().convert("RGB")).copy()
        finally:
            bitmap.close()
            page.close()


@pytest.mark.parametrize(
    ("kind", "extra", "color"),
    [
        ("text", {}, (0.60, 0.05, 0.30)),
        ("ref_text", {}, (0.45, 0.20, 0.65)),
        ("doc_title", {"level": 1}, (0.20, 0.20, 0.80)),
        ("paragraph_title", {"level": 2}, (0.20, 0.40, 0.90)),
        ("equation", {"content": "x^2"}, (0.00, 0.60, 0.10)),
        ("page_footnote", {}, (0, 128 / 255, 128 / 255)),
        *[(kind, {}, (158 / 255,) * 3) for kind in ("header", "footer", "page_number", "aside_text")],
    ],
)
def test_outline_styles_preserve_source_and_add_colored_labels(kind: str, extra: dict[str, Any], color: tuple) -> None:
    """Verify the color matching of the main text, footnotes and auxiliary blocks, only add a white background to the labels and retain the original text and hollow borders."""
    source = _source_pdf()
    result = render_layout_pdf(source, [_page(blocks=[_text_block(type=kind, **extra)])])
    assert _outlines(result)[0][0] == pytest.approx(color, abs=1e-6)
    assert _outlines(result)[0][1] == (20, 180, 100, 60)
    page = PdfReader(BytesIO(result)).pages[0]
    assert page.extract_text().splitlines() == ["Original page 0", f"{kind}: 0"]
    assert any(operator == b"w" and operands == [1] for operands, operator in page.get_contents().operations)
    backgrounds = _label_backgrounds(result)
    assert len(backgrounds) == 1
    x, y, width, height = backgrounds[0]
    assert (x, y) == (20, 241)
    assert width < 100 and height < 10
    fills = [operands for operands, operator in page.get_contents().operations if operator == b"rg"]
    assert tuple(fills[-1]) == pytest.approx(color, abs=1e-6)


@pytest.mark.parametrize(
    ("kind", "color"),
    [
        ("image", (0.30, 0.85, 0.05)),
        ("table", (0.80, 0.80, 0.00)),
        ("chart", (0.30, 0.85, 0.05)),
        ("code", (0.40, 0.00, 0.80)),
    ],
)
def test_visual_parent_draws_only_positioned_children(kind: str, color: tuple) -> None:
    """Chart and code parent blocks do not repeat frames, child blocks missing bbox are safely skipped."""
    body = {"type": f"{kind}_body", "index": 0, "bbox": (0.1, 0.2, 0.6, 0.4), "content": ""}
    caption = _text_block(1, type=f"{kind}_caption", bbox=(0.1, 0.5, 0.6, 0.6))
    footnote = _text_block(2, type=f"{kind}_footnote", bbox=None)
    parent = {"type": kind, "index": 0, "bbox": body["bbox"], "content": [body, caption, footnote]}
    if kind == "code":
        parent["sub_type"] = "code"
        parent["guess_lang"] = "python"
    output = render_layout_pdf(_source_pdf(), [_page(blocks=[parent])])
    outlines = _outlines(output)
    assert len(outlines) == 2
    assert outlines[0] == (color, (20, 180, 100, 60))
    assert outlines[1][1] == (20, 120, 100, 30)
    assert PdfReader(BytesIO(output)).pages[0].extract_text().splitlines() == [
        "Original page 0",
        f"{kind}_body: 0",
        f"{kind}_caption: 1",
    ]


@pytest.mark.parametrize("kind", ["list", "index"])
def test_nested_lists_and_indices_keep_hierarchy(kind: str) -> None:
    """Preserves parent-child boxes for lists and indexes, and traverses intermediate nodes without bbox."""
    nested = {"type": kind, "content": [_text_block(bbox=(0.2, 0.25, 0.5, 0.35))]}
    parent = {"type": kind, "index": 0, "bbox": (0.1, 0.2, 0.6, 0.4), "content": [nested]}
    outlines = _outlines(render_layout_pdf(_source_pdf(), [_page(blocks=[parent])]))
    assert outlines == [((0.1, 0.6, 0.35), (20, 180, 100, 60)), ((0.6, 0.05, 0.3), (40, 195, 60, 30))]


def test_labels_keep_sparse_indices_missing_children_and_algorithm_body() -> None:
    """Zero values, skip numbers, and parent-child duplicate numbers are retained; missing child numbers are not inherited, and the main body of the algorithm retains the true type."""
    blocks = [
        {
            "type": "list",
            "index": 0,
            "bbox": (0.1, 0.2, 0.6, 0.4),
            "content": [_text_block(index=None), _text_block(index=0, bbox=(0.1, 0.3, 0.6, 0.4))],
        },
        _text_block(index=7, bbox=(0.1, 0.45, 0.6, 0.5)),
        {
            "type": "code",
            "sub_type": "algorithm",
            "index": 12,
            "bbox": (0.1, 0.6, 0.6, 0.7),
            "content": [
                {
                    "type": "algorithm_body",
                    "index": 12,
                    "bbox": (0.1, 0.6, 0.6, 0.7),
                    "content": [{"type": "equation_inline", "content": "x+1"}],
                }
            ],
        },
    ]
    page = _page(blocks=blocks)
    before = page.model_dump(mode="json")
    output = render_layout_pdf(_source_pdf(), [page])
    assert page.model_dump(mode="json") == before
    assert PdfReader(BytesIO(output)).pages[0].extract_text().splitlines() == [
        "Original page 0",
        "list: 0text: -",
        "text: 0",
        "text: 7",
        "algorithm_body: 12",
    ]
    assert _outlines(output)[-1][0] == (0.4, 0, 0.8)


@pytest.mark.parametrize("top", [0, 0.2])
def test_labels_stay_at_block_top_with_only_page_edge_clamping(top: float) -> None:
    """Labels in the same box remain at the same height, and the top and right sides of the page are only condensed to the nearest location and no longer avoid crossing lines."""
    bbox = (0.95, top, 1.0, top + 0.1)
    children = [_text_block(index=index, bbox=bbox) for index in range(3)]
    parent = {"type": "list", "index": 0, "bbox": bbox, "content": children}
    output = render_layout_pdf(_source_pdf(), [_page(blocks=[parent])])
    backgrounds = _label_backgrounds(output)
    assert len(backgrounds) == 4
    for x, y, width, height in backgrounds:
        assert x >= 0 and y >= 0
        assert x + width <= 200.00001 and y + height <= 300.00001
        assert y == pytest.approx(min((1 - top) * 300 + 1, 300 - height), abs=1e-5)
    assert len({round(y, 5) for _, y, _, _ in backgrounds}) == 1
    assert all(rect == pytest.approx((190, (1 - top - 0.1) * 300, 10, 30)) for _, rect in _outlines(output))


@pytest.mark.parametrize("reverse", [False, True])
def test_dense_labels_remain_attached_to_each_block_regardless_of_order(reverse: bool) -> None:
    """Even if the tags of dense short paragraphs overlap each other, they maintain the corresponding upper boundary, and the input order cannot change the anchor point."""
    blocks = [_text_block(index=index, bbox=(0.1, 0.2 + index * 0.015, 0.6, 0.21 + index * 0.015)) for index in range(30)]
    if reverse:
        blocks.reverse()
        # Schema requires index to be incremented; the inversion is the physical position, and renumbering still overrides the reverse draw order.
        for index, block in enumerate(blocks):
            block["index"] = index
    output = render_layout_pdf(_source_pdf(), [_page(blocks=blocks)])
    backgrounds = _label_backgrounds(output)
    assert len(backgrounds) == len(blocks)
    for block, (x, y, _width, _height) in zip(blocks, backgrounds, strict=True):
        assert x == 20
        assert y == pytest.approx((1 - block["bbox"][1]) * 300 + 1, abs=1e-5)


@pytest.mark.parametrize("blocks", [[], [_text_block(bbox=None)]])
def test_unmarked_pages_preserve_original_content(blocks: list[dict[str, Any]]) -> None:
    """Empty pages and pages missing bbox retain the original content stream."""
    source = _source_pdf()
    output = render_layout_pdf(source, [_page(blocks=blocks)])
    assert not _outlines(output)
    assert (
        PdfReader(BytesIO(output)).pages[0].get_contents().get_data()
        == PdfReader(BytesIO(source)).pages[0].get_contents().get_data()
    )


def test_mapping_matches_original_indices_and_never_falls_back() -> None:
    """Complete PDF is positioned according to the original page number, rearranges the extracted pages and maps them explicitly, and missing results do not borrow adjacent pages."""
    source = _source_pdf(3)
    pages = [_page(1), _page(4, [_text_block(bbox=(0.2, 0.5, 0.4, 0.8))])]
    full = render_layout_pdf(source, pages)
    assert not _outlines(full, 0)
    assert _outlines(full, 1)[0][1] == (20, 180, 100, 60)
    assert not _outlines(full, 2)
    cropped = render_layout_pdf(source, pages, page_indices=(4, 9, 1))
    assert _outlines(cropped, 0)[0][1] == (40, 60, 40, 90)
    assert not _outlines(cropped, 1)
    assert _outlines(cropped, 2)[0][1] == (20, 180, 100, 60)


@pytest.mark.parametrize("indices", [(), (0, 1), (-1,), (True,), ("0",)])
def test_invalid_mapping_is_rejected(indices: tuple) -> None:
    """Reject mismatched page numbers or illegal original page numbers to avoid silently generating erroneous annotations."""
    with pytest.raises(ValueError, match="page_indices"):
        render_layout_pdf(_source_pdf(), [_page()], page_indices=indices)


def test_page_sequence_requires_current_unique_page_info() -> None:
    """The public entrance rejects original dictionaries and duplicate page numbers to avoid ambiguity."""
    with pytest.raises(TypeError, match="PageInfo"):
        render_layout_pdf(_source_pdf(), [{}])
    with pytest.raises(ValueError, match="unique"):
        render_layout_pdf(_source_pdf(), [_page(), _page()])


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize(
    ("media", "crop"),
    [
        ((0, 0, 300, 400), (0, 0, 200, 300)),
        ((0, 0, 300, 400), (20, 30, 220, 330)),
        ((-100, -150, 200, 250), (-80, -120, 120, 180)),
    ],
)
def test_rendered_pixels_match_display_bbox_for_rotations_and_offsets(rotation: int, media: tuple, crop: tuple) -> None:
    """Measure the border position using actual rendering with PDFium, and independently verify four rotations and positive and negative cropping offsets."""
    source = _source_pdf(rotation=rotation, media=media, crop=crop)
    page_info = _page()
    before = page_info.model_dump(mode="json")
    output = render_layout_pdf(source, [page_info])
    assert page_info.model_dump(mode="json") == before
    result_page = PdfReader(BytesIO(output)).pages[0]
    assert tuple(result_page.mediabox) == media
    assert tuple(result_page.cropbox) == crop
    assert result_page.rotation == rotation
    assert result_page["/Annots"][0].get_object()["/A"]["/URI"] == "https://example.com/"
    assert [line.strip() for line in result_page.extract_text().splitlines()] == ["Original page 0", "text: 0"]
    pixels = _render_pixels(output)
    red, green, blue = pixels[:, :, 0], pixels[:, :, 1], pixels[:, :, 2]
    mask = (red > 100) & (red < 220) & (green < 70) & (blue > 40) & (blue < 160)
    width, height = pixels.shape[1], pixels.shape[0]
    # The label is placed above the box, and the pixel range of the border is measured separately to avoid adding text that changes the bounding box of the geometric regression.
    outline_mask = mask.copy()
    outline_mask[: round(height * 0.2) - 2] = False
    y, x = np.where(outline_mask)
    assert (width, height) == ((600, 400) if rotation in (90, 270) else (400, 600))
    assert (x.min(), y.min(), x.max(), y.max()) == pytest.approx((width * 0.1, height * 0.2, width * 0.6, height * 0.4), abs=2)
    label_mask = mask[: round(height * 0.2) - 3]
    ly, lx = np.where(label_mask)
    assert lx.size > 0
    assert lx.min() == pytest.approx(width * 0.1 + 2, abs=2)
    assert ly.max() < height * 0.2 - 3
    # The same label should get the same glyph under all Rotate/CropBoxs, not just colored pixels.
    reference = _render_pixels(render_layout_pdf(_source_pdf(), [_page()]))
    r, g, b = reference[:, :, 0], reference[:, :, 1], reference[:, :, 2]
    reference_mask = ((r > 100) & (r < 220) & (g < 70) & (b > 40) & (b < 160))[:117]
    ry, rx = np.where(reference_mask)
    assert np.array_equal(
        label_mask[ly.min() : ly.max() + 1, lx.min() : lx.max() + 1],
        reference_mask[ry.min() : ry.max() + 1, rx.min() : rx.max() + 1],
    )


def test_pdf_document_wrapper_matches_public_renderer(tmp_path: Path) -> None:
    """The old public method consumes the new version of PageInfo and keeps the same page and stroke content as the byte interface."""
    source = _source_pdf(rotation=90, crop=(20, 30, 220, 330))
    pages = [_page(4)]
    expected = render_layout_pdf(source, pages, page_indices=(4,))
    output_path = tmp_path / "nested" / "layout.pdf"
    with PDFDocument(source) as document:
        document.draw_layout_bbox(pages, str(output_path), page_indices=(4,))
    actual = output_path.read_bytes()
    assert _outlines(actual) == _outlines(expected)
    assert PdfReader(BytesIO(actual)).pages[0].extract_text() == PdfReader(BytesIO(expected)).pages[0].extract_text()
    assert PdfReader(BytesIO(actual)).pages[0].rotation == 90
