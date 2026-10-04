from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    TextStringObject,
)
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from docvortex.document.pdf import _document as pdf_document
from docvortex.document.pdf import native_annotations, native_objects, native_text_geometry
from docvortex.document.pdf.text._contracts import Bbox


def test_pdf_document_does_not_expose_span_bbox_visualization() -> None:
    """Verify that exposing PDFDocument no longer exposes the unsupported span bbox drawing interface."""

    assert not hasattr(pdf_document.PDFDocument, "draw_span_bbox")


def test_pdf_page_exposes_path_infos_without_raw_pdfium_access() -> None:
    """Verify PDFPage read-only agent current page Path summary and preserve page index."""

    document = MagicMock()
    expected = [MagicMock()]
    document.get_page_path_infos.return_value = expected

    assert pdf_document.PDFPage(document, 3).get_path_infos() is expected
    document.get_page_path_infos.assert_called_once_with(3)


def test_pdf_page_exposes_chars_with_geometry_without_raw_pdfium_access() -> None:
    """Verify PDFPage agent expands character geometry and preserves page index."""
    document = MagicMock()
    expected = pdf_document.PDFPageTextGeometry(chars=[], tight_bboxes={}, origins={})
    document.get_page_chars_with_geometry.return_value = expected

    assert pdf_document.PDFPage(document, 4).get_chars_with_geometry() is expected
    document.get_page_chars_with_geometry.assert_called_once_with(4)


def _build_drawing_pdf() -> bytes:
    """Constructs a test PDF containing a stroke, a thin filled rectangle, adjacent line segments, a Form matrix, and a diagonal line."""
    output = BytesIO()
    canvas = Canvas(output, pagesize=(100, 200))
    canvas.setLineWidth(1)
    canvas.line(10, 180, 90, 180)
    canvas.line(10, 160, 50, 160)
    canvas.line(51, 160, 90, 160)
    canvas.rect(10, 139.5, 80, 0.5, stroke=0, fill=1)

    canvas.beginForm("NestedLine", 0, 0, 20, 10)
    canvas.setLineWidth(1)
    canvas.line(0, 0, 20, 0)
    canvas.endForm()
    canvas.saveState()
    canvas.translate(30, 100)
    canvas.scale(2, 1)
    canvas.doForm("NestedLine")
    canvas.restoreState()

    # Strokes with alpha 0 are not visible and public interfaces should be filtered.
    canvas.saveState()
    canvas.setStrokeAlpha(0)
    canvas.line(10, 120, 90, 120)
    canvas.restoreState()

    # Slashes do not belong to horizontal and vertical lines in the table, and the public interface should be filtered.
    canvas.line(10, 10, 90, 50)
    # Closed Bezier is used to verify that the Path information preserves the geometric extent formed by the control points.
    curve = canvas.beginPath()
    curve.moveTo(10, 80)
    curve.curveTo(20, 95, 30, 95, 40, 80)
    curve.lineTo(10, 80)
    curve.close()
    canvas.drawPath(curve, stroke=0, fill=1)
    canvas.save()
    return output.getvalue()


def _build_rotated_cropped_drawing_pdf() -> bytes:
    """Construct a test PDF with CropBox and 90 degree page rotation."""
    source = BytesIO()
    canvas = Canvas(source, pagesize=(100, 200))
    canvas.setLineWidth(2)
    canvas.line(10, 20, 90, 20)
    canvas.save()

    reader = PdfReader(BytesIO(source.getvalue()))
    page = reader.pages[0]
    page.rotate(90)
    page.cropbox.lower_left = (5, 10)
    page.cropbox.upper_right = (95, 190)
    writer = PdfWriter()
    writer.add_page(page)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _build_colored_path_pdf() -> bytes:
    """Construct visible light fill and transparent fill Path, verify RGBA metadata."""

    output = BytesIO()
    canvas = Canvas(output, pagesize=(100, 100))
    canvas.setFillColorRGB(242 / 255, 255 / 255, 242 / 255)
    canvas.rect(10, 60, 80, 20, stroke=0, fill=1)
    canvas.saveState()
    canvas.setFillAlpha(0)
    canvas.rect(10, 20, 80, 20, stroke=0, fill=1)
    canvas.restoreState()
    canvas.save()
    return output.getvalue()


def _build_rotated_cropped_image_pdf() -> bytes:
    """Construct normal, nested Form, partial off-page and fully off-page bitmaps and apply CropBox with rotation."""
    image = Image.new("RGB", (3, 4), "red")
    image_buffer = BytesIO()
    image.save(image_buffer, format="PNG")
    image_buffer.seek(0)
    image_reader = ImageReader(image_buffer)

    source = BytesIO()
    canvas = Canvas(source, pagesize=(100, 200))
    canvas.drawImage(image_reader, 10, 20, width=30, height=40)
    canvas.drawImage(image_reader, -10, 170, width=30, height=40)
    canvas.drawImage(image_reader, -30, -30, width=5, height=5)
    canvas.beginForm("NestedImage", 0, 0, 20, 20)
    canvas.drawImage(image_reader, 1, 2, width=3, height=4)
    canvas.endForm()
    canvas.saveState()
    canvas.translate(50, 80)
    canvas.scale(2, 3)
    canvas.doForm("NestedImage")
    canvas.restoreState()
    canvas.save()

    reader = PdfReader(BytesIO(source.getvalue()))
    page = reader.pages[0]
    page.rotate(90)
    page.cropbox.lower_left = (5, 10)
    page.cropbox.upper_right = (95, 190)
    writer = PdfWriter()
    writer.add_page(page)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _build_rotated_cropped_signature_pdf() -> bytes:
    """Construct CropBox rotation test PDF with visible signature and various invalid annotations."""

    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=200)
    page.rotate(90)
    page.cropbox.lower_left = (5, 10)
    page.cropbox.upper_right = (95, 190)

    def add_appearance(width: float, height: float) -> object:
        """Create a minimal normal Form appearance stream for the test signature."""

        appearance = DecodedStreamObject()
        appearance.set_data(b"q 1 0 0 rg 0 0 1 1 re f Q")
        appearance.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Form"),
                NameObject("/BBox"): ArrayObject(
                    [
                        FloatObject(0),
                        FloatObject(0),
                        FloatObject(width),
                        FloatObject(height),
                    ]
                ),
                NameObject("/Resources"): DictionaryObject(),
            }
        )
        return writer._add_object(appearance)

    annotations = ArrayObject()
    form_fields = ArrayObject()

    def add_widget(
        rect: tuple[float, float, float, float],
        *,
        flags: int = 4,
        field_type: str = "/Sig",
        subtype: str = "/Widget",
        with_appearance: bool = True,
        inherited_field_type: bool = False,
    ) -> None:
        """Append a configurable test Widget covering the visibility and structure filter branches."""

        annotation = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject(subtype),
                NameObject("/Rect"): ArrayObject([FloatObject(value) for value in rect]),
                NameObject("/F"): NumberObject(flags),
            }
        )
        parent_field = None
        parent_reference = None
        if inherited_field_type:
            parent_field = DictionaryObject(
                {
                    NameObject("/FT"): NameObject(field_type),
                    NameObject("/T"): TextStringObject("InheritedSignature"),
                }
            )
            parent_reference = writer._add_object(parent_field)
            annotation[NameObject("/Parent")] = parent_reference
        else:
            annotation[NameObject("/FT")] = NameObject(field_type)
        if with_appearance:
            annotation[NameObject("/AP")] = DictionaryObject(
                {
                    NameObject("/N"): add_appearance(
                        abs(rect[2] - rect[0]),
                        abs(rect[3] - rect[1]),
                    )
                }
            )
        annotation_reference = writer._add_object(annotation)
        annotations.append(annotation_reference)
        if parent_field is not None and parent_reference is not None:
            parent_field[NameObject("/Kids")] = ArrayObject([annotation_reference])
            form_fields.append(parent_reference)
        elif subtype == "/Widget":
            form_fields.append(annotation_reference)

    add_widget((10, 20, 40, 60), inherited_field_type=True)
    add_widget((-10, 170, 20, 210))
    add_widget((20, 80, 40, 100), flags=pdf_document.pdfium_c.FPDF_ANNOT_FLAG_HIDDEN)
    add_widget((20, 80, 40, 100), flags=pdf_document.pdfium_c.FPDF_ANNOT_FLAG_INVISIBLE)
    add_widget((20, 80, 40, 100), flags=pdf_document.pdfium_c.FPDF_ANNOT_FLAG_NOVIEW)
    add_widget((20, 80, 40, 100), with_appearance=False)
    add_widget((20, 80, 40, 100), field_type="/Btn")
    add_widget((20, 80, 40, 100), subtype="/Text")
    page[NameObject("/Annots")] = annotations
    writer._root_object[NameObject("/AcroForm")] = DictionaryObject({NameObject("/Fields"): form_fields})

    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _build_rotated_cropped_link_pdf() -> bytes:
    """Construct a rotational Link annotation test PDF with QuadPoints, Rect fallback and invalid actions."""

    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=200)
    page.rotate(90)
    page.cropbox.lower_left = (5, 10)
    page.cropbox.upper_right = (95, 190)
    annotations = ArrayObject()

    def add_uri_link(
        target: str,
        rect: tuple[float, float, float, float],
        *,
        quad_points: tuple[float, ...] | None = None,
        flags: int = 0,
    ) -> None:
        """Append a configurable URI Link to override the target checksum area read branch."""

        annotation = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Link"),
                NameObject("/Rect"): ArrayObject([FloatObject(value) for value in rect]),
                NameObject("/A"): DictionaryObject(
                    {
                        NameObject("/S"): NameObject("/URI"),
                        NameObject("/URI"): TextStringObject(target),
                    }
                ),
            }
        )
        if quad_points is not None:
            annotation[NameObject("/QuadPoints")] = ArrayObject([FloatObject(value) for value in quad_points])
        if flags:
            annotation[NameObject("/F")] = NumberObject(flags)
        annotations.append(writer._add_object(annotation))

    add_uri_link(
        "https://example.com/a?x=1&y=2",
        (10, 20, 60, 40),
        quad_points=(20, 35, 40, 35, 20, 25, 40, 25),
    )
    add_uri_link("mailto:user@example.com", (10, 50, 40, 60))
    add_uri_link("tel:+123456", (10, 70, 40, 80))
    add_uri_link("javascript:alert(1)", (10, 90, 40, 100))
    add_uri_link("relative/path", (10, 110, 40, 120))
    add_uri_link(
        "https://hidden.example.com",
        (10, 130, 40, 140),
        flags=pdf_document.pdfium_c.FPDF_ANNOT_FLAG_HIDDEN,
    )
    add_uri_link(
        "https://invisible.example.com",
        (45, 130, 75, 140),
        flags=pdf_document.pdfium_c.FPDF_ANNOT_FLAG_INVISIBLE,
    )
    add_uri_link(
        "https://noview.example.com",
        (10, 145, 40, 155),
        flags=pdf_document.pdfium_c.FPDF_ANNOT_FLAG_NOVIEW,
    )
    annotations.append(
        writer._add_object(
            DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Annot"),
                    NameObject("/Subtype"): NameObject("/Link"),
                    NameObject("/Rect"): ArrayObject([FloatObject(10), FloatObject(20)]),
                    NameObject("/A"): DictionaryObject(
                        {
                            NameObject("/S"): NameObject("/URI"),
                            NameObject("/URI"): TextStringObject("https://broken.example.com"),
                        }
                    ),
                }
            )
        )
    )
    annotations.append(
        writer._add_object(
            DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Annot"),
                    NameObject("/Subtype"): NameObject("/Link"),
                    NameObject("/Rect"): ArrayObject(
                        [
                            FloatObject(10),
                            FloatObject(165),
                            FloatObject(40),
                            FloatObject(175),
                        ]
                    ),
                    NameObject("/Dest"): TextStringObject("missing-destination"),
                }
            )
        )
    )
    page[NameObject("/Annots")] = annotations

    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class _TrackingLock:
    def __init__(self) -> None:
        self.depth = 0

    def __enter__(self) -> None:
        self.depth += 1

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.depth -= 1


def test_pdf_document_methods_keep_page_access_inside_pdfium_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    lock = _TrackingLock()
    monkeypatch.setattr(pdf_document, "_pdfium_lock", lock)
    # The operation entrance and cleanup entrance must use the same shared lock to allow reentrancy during initialization and cleanup.
    from docvortex.document.pdf import pdfium as pdfium_runtime

    monkeypatch.setattr(pdfium_runtime, "_pdfium_lock", lock)

    events: list[str] = []

    class _FakeBitmap:
        def to_pil(self) -> Image.Image:
            events.append(f"bitmap.to_pil:{lock.depth}")
            return Image.new("RGB", (2, 2), "white")

        def close(self) -> None:
            events.append(f"bitmap.close:{lock.depth}")

    class _FakePage:
        def get_bbox(self) -> tuple[float, float, float, float]:
            events.append(f"page.get_bbox:{lock.depth}")
            return (0.0, 10.0, 20.0, 0.0)

        def get_size(self) -> tuple[int, int]:
            events.append(f"page.get_size:{lock.depth}")
            return 20, 10

        def get_textpage(self) -> "_FakeTextPage":
            events.append(f"page.get_textpage:{lock.depth}")
            return _FakeTextPage()

        def get_rotation(self) -> int:
            events.append(f"page.get_rotation:{lock.depth}")
            return 0

        def render(self, *, scale: float) -> _FakeBitmap:
            events.append(f"page.render:{lock.depth}:{scale}")
            return _FakeBitmap()

    class _FakeTextPage:
        def close(self) -> None:
            events.append(f"textpage.close:{lock.depth}")

    class _FakeDoc:
        def __init__(self, pdf_bytes: bytes) -> None:
            events.append(f"doc.open:{lock.depth}:{pdf_bytes!r}")
            self.page = _FakePage()
            self.raw = object()

        def __len__(self) -> int:
            events.append(f"doc.__len__:{lock.depth}")
            return 1

        def __getitem__(self, page_idx: int) -> _FakePage:
            events.append(f"doc.__getitem__:{lock.depth}:{page_idx}")
            return self.page

        def close(self) -> None:
            events.append(f"doc.close:{lock.depth}")

    def fake_get_chars(
        textpage: _FakeTextPage, page_bbox: list[float], page_rotation: int, *, include_geometry: bool = False
    ) -> list[dict[str, Any]]:
        """Record lock depth when extracting text to avoid relying on the old module-level get_page_chars hook."""
        events.append(f"get_chars:{lock.depth}:{page_bbox}:{page_rotation}")
        return [
            {
                "char": "A",
                "bbox": Bbox([0.0, 0.0, 1.0, 1.0]),
                "rotation": 0,
                "font": {"name": "Helvetica", "flags": 0, "size": 10, "weight": 400},
                "char_idx": 0,
                "raw_code": 65,
                "source_indices": (0,),
            }
        ]

    def fake_extract_page_drawing_lines(
        page: _FakePage,
        page_bbox: tuple[float, float, float, float],
        page_rotation: int,
    ) -> list[pdf_document.PDFDrawingLine]:
        """The PDFium lock is still held by PDFDocument while recording the drawing object traversal."""
        events.append(f"drawing_lines:{lock.depth}:{page_bbox}:{page_rotation}")
        return []

    def fake_extract_page_image_bboxes(
        page: _FakePage,
        page_bbox: tuple[float, float, float, float],
        page_rotation: int,
    ) -> list[tuple[float, float, float, float]]:
        """The PDFium lock is still held by PDFDocument while recording the bitmap traversal."""
        events.append(f"image_bboxes:{lock.depth}:{page_bbox}:{page_rotation}")
        return []

    def fake_extract_page_image_infos(
        page: _FakePage,
        page_bbox: tuple[float, float, float, float],
        page_rotation: int,
    ) -> list[pdf_document.PDFImageInfo]:
        """The PDFium lock is still held by PDFDocument when recording image fingerprint metadata traversal."""

        events.append(f"image_infos:{lock.depth}:{page_bbox}:{page_rotation}")
        return []

    def fake_extract_page_path_infos(
        page: _FakePage,
        page_bbox: tuple[float, float, float, float],
        page_rotation: int,
    ) -> list[pdf_document.PDFPathInfo]:
        """The PDFium lock is still held by PDFDocument while the fully logged Path geometry is traversed."""
        events.append(f"path_infos:{lock.depth}:{page_bbox}:{page_rotation}")
        return []

    def fake_extract_page_form_bboxes(
        page: _FakePage,
        page_bbox: tuple[float, float, float, float],
        page_rotation: int,
    ) -> list[tuple[float, float, float, float]]:
        """The PDFium lock is still held by PDFDocument while logging Form is traversed."""
        events.append(f"form_bboxes:{lock.depth}:{page_bbox}:{page_rotation}")
        return []

    def fake_extract_page_signature_bboxes(
        page: _FakePage,
        page_bbox: tuple[float, float, float, float],
        page_rotation: int,
        *,
        form_handle: object | None = None,
    ) -> list[tuple[float, float, float, float]]:
        """The PDFium lock is still held by PDFDocument while the record signature annotation is traversed."""

        assert form_handle is None
        events.append(f"signature_bboxes:{lock.depth}:{page_bbox}:{page_rotation}")
        return []

    def fake_extract_page_link_annotations(
        page: _FakePage,
        raw_doc: object,
        page_bbox: tuple[float, float, float, float],
        page_rotation: int,
    ) -> list[pdf_document.PDFLinkAnnotation]:
        """Logging Link The page and document handles were still within the PDFium lock while the annotation was traversed."""

        assert raw_doc is doc._pdf_doc.raw
        events.append(f"link_annotations:{lock.depth}:{page_bbox}:{page_rotation}")
        return []

    monkeypatch.setattr(pdf_document.pdfium, "PdfDocument", _FakeDoc)
    monkeypatch.setattr(native_text_geometry, "get_chars", fake_get_chars, raising=False)
    monkeypatch.setattr(pdf_document, "_extract_page_drawing_lines", fake_extract_page_drawing_lines)
    monkeypatch.setattr(pdf_document, "_extract_page_path_infos", fake_extract_page_path_infos)
    monkeypatch.setattr(pdf_document, "_extract_page_image_bboxes", fake_extract_page_image_bboxes)
    monkeypatch.setattr(pdf_document, "_extract_page_image_infos", fake_extract_page_image_infos)
    monkeypatch.setattr(pdf_document, "_extract_page_form_bboxes", fake_extract_page_form_bboxes)
    monkeypatch.setattr(pdf_document, "_extract_page_signature_bboxes", fake_extract_page_signature_bboxes)
    monkeypatch.setattr(pdf_document, "_extract_page_link_annotations", fake_extract_page_link_annotations)

    doc = pdf_document.PDFDocument(b"%PDF")

    assert doc.page_size(0) == (20.0, 10.0)
    image = doc.render_page(0, scale=3)
    assert image.pil_image.size == (2, 2)
    assert image.scale == 3
    assert doc.get_page_chars(0)[0]["char"] == "A"
    assert doc.get_page_drawing_lines(0) == []
    assert doc.get_page_path_infos(0) == []
    assert doc.get_page_image_bboxes(0) == []
    assert doc.get_page_image_infos(0) == []
    assert doc.get_page_form_bboxes(0) == []
    assert doc.get_page_signature_bboxes(0) == []
    assert doc.get_page_link_annotations(0) == []

    assert any(event.startswith("doc.open:") and not event.startswith("doc.open:0:") for event in events)
    assert any(event.startswith("doc.__getitem__:") and not event.startswith("doc.__getitem__:0:") for event in events)
    assert "page.get_bbox:1" in events
    assert "page.get_size:1" in events
    assert "page.render:1:3" in events
    assert "bitmap.to_pil:1" in events
    assert "bitmap.close:1" in events
    assert "page.get_textpage:1" in events
    assert "get_chars:1:[0.0, 10.0, 20.0, 0.0]:0" in events
    assert "textpage.close:1" in events
    assert "drawing_lines:1:(0.0, 0.0, 20.0, 10.0):0" in events
    assert "path_infos:1:(0.0, 0.0, 20.0, 10.0):0" in events
    assert "image_bboxes:1:(0.0, 0.0, 20.0, 10.0):0" in events
    assert "image_infos:1:(0.0, 0.0, 20.0, 10.0):0" in events
    assert "form_bboxes:1:(0.0, 0.0, 20.0, 10.0):0" in events
    assert "signature_bboxes:1:(0.0, 0.0, 20.0, 10.0):0" in events
    assert "link_annotations:1:(0.0, 0.0, 20.0, 10.0):0" in events


def test_pdf_document_does_not_expose_legacy_compat_hooks() -> None:
    assert not hasattr(pdf_document, "pdf_page_to_image")
    assert not hasattr(pdf_document, "open_pdfium_document")
    assert not hasattr(pdf_document.PDFDocument, "get_text_quality")
    assert pdf_document.PDFDocument._pdf_doc.fset is None


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [
        (0, (10.0, 150.0, 30.0, 170.0)),
        (90, (30.0, 10.0, 50.0, 30.0)),
        (180, (70.0, 30.0, 90.0, 50.0)),
        (270, (150.0, 70.0, 170.0, 90.0)),
    ],
)
def test_char_visual_bbox_from_pdfium_applies_page_rotation(
    rotation: int,
    expected: tuple[float, float, float, float],
) -> None:
    """Verify tight char box converts by non-zero CropBox and four page rotations."""
    assert (
        pdf_document._char_visual_bbox_from_pdfium(
            20.0,
            40.0,
            50.0,
            70.0,
            (10.0, 20.0, 110.0, 220.0),
            rotation,
        )
        == expected
    )


def test_extract_page_char_extended_geometry_isolates_single_char_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure to verify tight/origin single character or illegal value does not affect the remaining characters on the same page."""

    class _FakeTextPage:
        raw = object()

    chars = [
        {
            "char": text,
            "bbox": Bbox([float(index), 0.0, float(index + 1), 1.0]),
            "rotation": 0.1 if index == 0 else 0.0,
            "font": {},
            "char_idx": index,
        }
        for index, text in enumerate("AB")
    ]

    def fake_get_char_box(
        _textpage: object,
        index: int,
        left: Any,
        right: Any,
        bottom: Any,
        top: Any,
    ) -> bool:
        """Returns legal tight bbox only for the first character."""
        if index != 0:
            return False
        left.value, right.value, bottom.value, top.value = 10.0, 15.0, 180.0, 190.0
        return True

    def fake_get_loose_char_box(
        _textpage: object,
        index: int,
        rect: Any,
    ) -> bool:
        """Returns legal loose bbox only for the first character."""
        if index != 0:
            return False
        rect.left, rect.right, rect.bottom, rect.top = 9.0, 16.0, 179.0, 191.0
        return True

    def fake_get_char_origin(
        _textpage: object,
        index: int,
        origin_x: Any,
        origin_y: Any,
    ) -> bool:
        """The second character returns a non-limited origin, verify that it is discarded alone."""
        origin_x.value = 10.0 + index
        origin_y.value = 180.0 if index == 0 else float("nan")
        return True

    monkeypatch.setattr(pdf_document.pdfium_c, "FPDFText_GetLooseCharBox", fake_get_loose_char_box)
    monkeypatch.setattr(pdf_document.pdfium_c, "FPDFText_GetCharBox", fake_get_char_box)
    monkeypatch.setattr(pdf_document.pdfium_c, "FPDFText_GetCharOrigin", fake_get_char_origin)

    loose_bboxes, tight_bboxes, origins = pdf_document._extract_page_char_extended_geometry(
        _FakeTextPage(),  # type: ignore[arg-type]
        chars,  # type: ignore[arg-type]
        (0.0, 0.0, 100.0, 200.0),
        0,
    )

    assert loose_bboxes == {0: (9.0, 9.0, 16.0, 21.0)}
    assert tight_bboxes == {0: (10.0, 10.0, 15.0, 20.0)}
    assert origins == {0: (10.0, 20.0)}


def test_get_page_chars_with_geometry_preserves_legacy_char_output() -> None:
    """Verify that extended reads do not alter existing character text, indexes, or loose bbox."""
    output = BytesIO()
    canvas = Canvas(output, pagesize=(120, 80))
    canvas.drawString(10, 50, "Geometry A2")
    canvas.save()

    with pdf_document.PDFDocument(output.getvalue()) as document:
        legacy_chars = document.get_page_chars(0)
        geometry = document.get_page_chars_with_geometry(0)

    def snapshot(chars: list[dict[str, Any]]) -> list[tuple[str, int, tuple[float, ...]]]:
        """Generate stable character snapshots that ignore third-party bbox container types."""
        return [
            (
                str(char.get("char", "")),
                int(char.get("char_idx", -1)),
                tuple(float(value) for value in char["bbox"]),
            )
            for char in chars
        ]

    assert snapshot(geometry.chars) == snapshot(legacy_chars)  # type: ignore[arg-type]
    visible_indices = {int(char["char_idx"]) for char in geometry.chars if str(char.get("char", "")).strip()}
    assert geometry.loose_bboxes == {}
    assert visible_indices <= geometry.tight_bboxes.keys()
    assert visible_indices <= geometry.origins.keys()


def test_restore_pdfium_surrogate_pairs_recovers_supplementary_unicode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that the legal surrogate pair is recoverable, and the real replacement characters and isolated surrogate will not be misjudged."""

    raw_codes = [ord("A"), 0xD835, 0xDF03, 0xFFFD, 0xD835, ord("B")]

    class _FakeTextPage:
        raw = object()

        def count_chars(self) -> int:
            return len(raw_codes)

    monkeypatch.setattr(
        pdf_document.pdfium_c,
        "FPDFText_GetUnicode",
        lambda _textpage, char_idx: raw_codes[char_idx],
    )
    chars = [
        {
            "char": text,
            "bbox": Bbox([float(char_idx), 0.0, float(char_idx + 1), 1.0]),
            "rotation": 0,
            "font": {},
            "char_idx": char_idx,
        }
        for char_idx, text in enumerate(["A", "\ufffd", "\ufffd", "\ufffd", "\ud835", "B"])
    ]

    restored = pdf_document._restore_pdfium_surrogate_pairs(chars, _FakeTextPage())

    assert [char["char"] for char in restored] == ["A", "𝜃", "\ufffd", "\ufffd", "B"]
    assert [char["char_idx"] for char in restored] == [0, 1, 3, 4, 5]


def test_get_page_drawing_lines_extracts_forms_filled_rectangles_and_merges_segments() -> None:
    """Verify that the drawing line interface supports Form, slim filled rectangles, collinear merging, and filtered diagonal lines."""
    with pdf_document.PDFDocument(_build_drawing_pdf()) as doc:
        lines = doc.get_page_drawing_lines(0)

    assert [line.orientation for line in lines] == ["horizontal"] * 4
    assert [line.start for line in lines] == pytest.approx(
        [
            (10.0, 20.0),
            (10.0, 40.0),
            (10.0, 60.25),
            (30.0, 100.0),
        ]
    )
    assert [line.end for line in lines] == pytest.approx(
        [
            (90.0, 20.0),
            (90.0, 40.0),
            (90.0, 60.25),
            (70.0, 100.0),
        ]
    )
    assert [line.width for line in lines] == pytest.approx([1.0, 1.0, 0.5, 1.0])
    assert lines[2].bbox == pytest.approx((10.0, 60.0, 90.0, 60.5))


def test_get_page_path_infos_preserves_bezier_visibility_depth_and_source_order() -> None:
    """Verify that the complete Path interface retains the Bessel bbox, draw mode, Form depth and stable source number."""

    with pdf_document.PDFDocument(_build_drawing_pdf()) as doc:
        path_infos = doc.get_page_path_infos(0)

    assert len(path_infos) == 7
    assert [item.source_index for item in path_infos] == [0, 1, 2, 3, 4, 6, 7]
    assert path_infos[0].bbox == pytest.approx((9.5, 19.5, 90.5, 20.5))
    assert not path_infos[0].fill_visible and path_infos[0].stroke_visible
    assert path_infos[3].bbox == pytest.approx((10.0, 60.0, 90.0, 60.5))
    assert path_infos[3].fill_visible and not path_infos[3].stroke_visible
    assert path_infos[3].fill_rgba == (0, 0, 0, 255)
    nested_path = next(item for item in path_infos if item.form_depth == 1)
    # BBox of Form cuts off the lower half of the stroke and expands both ends; rendering verification only retains visible ink for x=30..70 and y<100.
    assert nested_path.bbox == pytest.approx((30.0, 99.0, 70.0, 100.0))
    bezier_path = path_infos[-1]
    assert bezier_path.segment_count == 5
    assert bezier_path.bbox == pytest.approx((10.0, 105.0, 40.0, 120.0))


def test_get_page_path_infos_exposes_fill_rgba_and_transparency() -> None:
    """Validate visible Path Preserve padding RGBA, transparent padding does not masquerade as visible background."""

    with pdf_document.PDFDocument(_build_colored_path_pdf()) as doc:
        path_infos = doc.get_page_path_infos(0)

    visible = next(item for item in path_infos if item.fill_visible)
    transparent = next(item for item in path_infos if not item.fill_visible)
    assert visible.fill_rgba == (242, 255, 242, 255)
    assert transparent.fill_rgba is None


def test_raw_object_rgba_failure_returns_none() -> None:
    """None is returned when verifying that old PDFium or corrupted objects fail to read color."""

    def broken_getter(*_args: Any) -> bool:
        """Simulate underlying color interface exceptions."""

        raise RuntimeError("broken color")

    assert pdf_document._get_raw_object_rgba(object(), broken_getter) is None


def test_get_page_drawing_lines_applies_crop_box_and_page_rotation() -> None:
    """Verification page CropBox with 90 degree rotation is converted to upper left origin coordinates."""
    with pdf_document.PDFDocument(_build_rotated_cropped_drawing_pdf()) as doc:
        page_size = doc.page_size(0)
        page_rotation = doc.page_rotation(0)
        page_object_rotation = doc[0].rotation
        lines = doc.get_page_drawing_lines(0)

    assert page_size == pytest.approx((180.0, 90.0))
    assert page_rotation == 90
    assert page_object_rotation == 90
    assert len(lines) == 1
    line = lines[0]
    assert line.orientation == "vertical"
    assert line.start == pytest.approx((10.0, 5.0))
    assert line.end == pytest.approx((10.0, 85.0))
    assert line.bbox == pytest.approx((9.0, 5.0, 11.0, 85.0))
    assert line.width == pytest.approx(2.0)


def test_get_page_path_infos_applies_crop_box_page_rotation_and_stroke_width() -> None:
    """Verify that Path bbox retains visible stroke width after CropBox and page rotation."""

    with pdf_document.PDFDocument(_build_rotated_cropped_drawing_pdf()) as doc:
        path_infos = doc.get_page_path_infos(0)

    assert len(path_infos) == 1
    assert path_infos[0].bbox == pytest.approx((9.0, 4.0, 11.0, 86.0))
    assert path_infos[0].form_depth == 0
    assert path_infos[0].segment_count == 2


def test_get_page_image_bboxes_applies_forms_crop_box_rotation_and_clipping() -> None:
    """Verify the bitmap interface recursively Form, and press CropBox, page rotation and crop to the upper left coordinate."""
    with pdf_document.PDFDocument(_build_rotated_cropped_image_pdf()) as doc:
        page_size = doc.page_size(0)
        image_bboxes = doc.get_page_image_bboxes(0)

    assert page_size == pytest.approx((180.0, 90.0))
    assert image_bboxes == pytest.approx(
        [
            (160.0, 0.0, 180.0, 15.0),
            (10.0, 5.0, 50.0, 35.0),
            (76.0, 47.0, 88.0, 53.0),
        ]
    )


def test_get_page_image_infos_preserves_bboxes_and_fingerprints_reused_images() -> None:
    """Verify that the image information maintains the existing geometry and generates the same content fingerprint for the normal and Form multiplex images."""

    with pdf_document.PDFDocument(_build_rotated_cropped_image_pdf()) as doc:
        image_infos = doc.get_page_image_infos(0)

    assert [info.bbox for info in image_infos] == pytest.approx(
        [
            (160.0, 0.0, 180.0, 15.0),
            (10.0, 5.0, 50.0, 35.0),
            (76.0, 47.0, 88.0, 53.0),
        ]
    )
    assert len({info.fingerprint for info in image_infos}) == 1
    assert image_infos[0].fingerprint is not None


def test_image_fingerprint_fails_open_when_raw_stream_exceeds_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that the oversized image stream does not allocate a buffer and returns an empty fingerprint. Subsequently, it will be released as a normal image."""

    raw_data_calls: list[tuple[object | None, int]] = []

    def fake_metadata(raw_obj: object, raw_page: object, metadata_pointer: Any) -> int:
        """Fill in the valid pixel width and height so that the test only hits the upper limit of the original stream size."""

        metadata_pointer._obj.width = 100
        metadata_pointer._obj.height = 200
        return 1

    def fake_raw_data(raw_obj: object, buffer: object | None, buffer_length: int) -> int:
        """Declare a stream that exceeds the cap and log if a second buffer read occurs."""

        raw_data_calls.append((buffer, buffer_length))
        return pdf_document.PDF_IMAGE_FINGERPRINT_MAX_RAW_BYTES + 1

    class _FakePage:
        """Provides the minimum raw page handle required by the PDFium metadata interface."""

        raw = object()

    monkeypatch.setattr(pdf_document.pdfium_c, "FPDFImageObj_GetImageMetadata", fake_metadata)
    monkeypatch.setattr(pdf_document.pdfium_c, "FPDFImageObj_GetImageDataRaw", fake_raw_data)

    assert pdf_document._get_raw_image_fingerprint(object(), _FakePage()) is None
    assert raw_data_calls == [(None, 0)]


def test_get_page_form_bboxes_reads_root_forms_and_nested_content_bounds() -> None:
    """Verify that the top-level Form bbox overwrites its nested drawing contents and does not duplicate the output of internal objects."""
    with pdf_document.PDFDocument(_build_drawing_pdf()) as doc:
        form_bboxes = doc.get_page_form_bboxes(0)

    assert form_bboxes == pytest.approx([(30.0, 99.0, 70.0, 100.0)])


def test_get_page_form_bboxes_applies_crop_box_rotation_and_clipping() -> None:
    """Verify Form bbox Press CropBox to convert with page rotation and crop to upper left origin coordinates."""
    with pdf_document.PDFDocument(_build_rotated_cropped_image_pdf()) as doc:
        page_size = doc.page_size(0)
        form_bboxes = doc.get_page_form_bboxes(0)

    assert page_size == pytest.approx((180.0, 90.0))
    assert form_bboxes == pytest.approx([(76.0, 47.0, 88.0, 53.0)])


def test_get_page_signature_bboxes_filters_visibility_and_applies_page_geometry() -> None:
    """Verify that only normal signatures are visible in the output, and that CropBox, rotation, and page cropping are applied correctly."""

    with pdf_document.PDFDocument(_build_rotated_cropped_signature_pdf()) as doc:
        page_size = doc.page_size(0)
        signature_bboxes = doc.get_page_signature_bboxes(0)

    assert page_size == pytest.approx((180.0, 90.0))
    assert signature_bboxes == pytest.approx(
        [
            (160.0, 0.0, 180.0, 15.0),
            (10.0, 5.0, 50.0, 35.0),
        ]
    )


def test_pdf_document_extracts_safe_external_link_annotations() -> None:
    """Verify external URI whitelist, QuadPoints priority and rotation CropBox coordinate transformation."""

    with pdf_document.PDFDocument(_build_rotated_cropped_link_pdf()) as document:
        page_size = document.page_size(0)
        links = document[0].get_link_annotations()

    assert page_size == pytest.approx((180.0, 90.0))
    assert [link.target for link in links] == [
        "https://example.com/a?x=1&y=2",
        "mailto:user@example.com",
        "tel:+123456",
    ]
    assert links[0].bboxes[0] == pytest.approx((15.0, 15.0, 25.0, 35.0))
    assert links[1].bboxes[0] == pytest.approx((40.0, 5.0, 50.0, 35.0))
    assert links[2].bboxes[0] == pytest.approx((60.0, 5.0, 70.0, 35.0))
    assert [link.source_index for link in links] == [0, 1, 2]


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("HTTP://Example.com/path", "HTTP://Example.com/path"),
        ("mailto:user@example.com", "mailto:user@example.com"),
        ("tel:+123456", "tel:+123456"),
        ("https:///missing-host", None),
        ("mailto:", None),
        ("javascript:alert(1)", None),
        ("relative/path", None),
        ("https://example.com/a\x01b", None),
    ],
)
def test_pdf_external_link_target_validation(
    target: str,
    expected: str | None,
) -> None:
    """Verification PDF producer only accepts explicit security protocols and complete targets."""

    assert pdf_document._validate_pdf_external_link_target(target) == expected


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [
        (0, (15.0, 155.0, 35.0, 165.0)),
        (90, (15.0, 15.0, 25.0, 35.0)),
        (180, (55.0, 15.0, 75.0, 25.0)),
        (270, (155.0, 55.0, 165.0, 75.0)),
    ],
)
def test_pdf_link_region_geometry_supports_standard_page_rotations(
    rotation: int,
    expected: tuple[float, float, float, float],
) -> None:
    """Verify that the Link point set is converted to unified visual coordinates in the four standard page orientations."""

    bbox = pdf_document._visual_bbox_from_pdf_points(
        [(20.0, 25.0), (40.0, 25.0), (20.0, 35.0), (40.0, 35.0)],
        (5.0, 10.0, 95.0, 190.0),
        rotation,
    )

    assert bbox == pytest.approx(expected)


def test_extract_page_signature_bboxes_closes_handles_and_skips_bad_annotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that the corrupted signature is isolated and the annotation handle is closed on either success or failure path."""

    bad_annot = object()
    good_annot = object()
    closed: list[object] = []

    class _FakePage:
        """Provides the minimum page handle required to annotate the original interface."""

        raw = object()

    def fake_get_annot(_raw_page: object, index: int) -> object:
        """Returns one corrupted comment and one valid comment by index."""

        return (bad_annot, good_annot)[index]

    def fake_signature_bbox(
        raw_annot: object,
        _page_bbox: tuple[float, float, float, float],
        _page_rotation: int,
        _form_handle: object | None,
    ) -> tuple[float, float, float, float]:
        """Let the first annotation throw an error and verify that the second annotation can still be extracted."""

        if raw_annot is bad_annot:
            raise RuntimeError("broken annotation")
        return (10.0, 20.0, 30.0, 40.0)

    monkeypatch.setattr(pdf_document.pdfium_c, "FPDFPage_GetAnnotCount", lambda _page: 2)
    monkeypatch.setattr(pdf_document.pdfium_c, "FPDFPage_GetAnnot", fake_get_annot)
    monkeypatch.setattr(pdf_document.pdfium_c, "FPDFPage_CloseAnnot", closed.append)
    monkeypatch.setattr(native_annotations, "_signature_bbox_from_annotation", fake_signature_bbox)

    assert pdf_document._extract_page_signature_bboxes(
        _FakePage(),
        (0.0, 0.0, 100.0, 200.0),
        0,
    ) == [(10.0, 20.0, 30.0, 40.0)]
    assert closed == [bad_annot, good_annot]


def test_extract_page_form_bboxes_skips_one_bad_object(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that a single corrupted Form does not block the extraction of other valid Forms on the same page."""
    bad_object = object()
    good_object = object()

    def fake_form_bbox(
        raw_object: object,
        page_bbox: tuple[float, float, float, float],
        page_rotation: int,
    ) -> tuple[float, float, float, float]:
        """The first object throws an error, and the second object returns verifiable bbox."""
        assert page_bbox == (0.0, 0.0, 100.0, 200.0)
        assert page_rotation == 0
        if raw_object is bad_object:
            raise RuntimeError("broken form")
        return (10.0, 20.0, 30.0, 40.0)

    def fake_root_forms(_page: object) -> Any:
        """Return damaged objects and valid objects in sequence to verify object-by-object exception isolation."""
        return iter((bad_object, good_object))

    monkeypatch.setattr(
        native_objects,
        "_iter_raw_root_form_objects",
        fake_root_forms,
    )
    monkeypatch.setattr(native_objects, "_form_bbox_from_object", fake_form_bbox)

    assert pdf_document._extract_page_form_bboxes(
        object(),
        (0.0, 0.0, 100.0, 200.0),
        0,
    ) == [(10.0, 20.0, 30.0, 40.0)]


def test_get_page_drawing_lines_skips_one_bad_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that a single Path parsing exception does not lose other valid plot lines on the same page."""
    original_extract = pdf_document._extract_path_drawing_lines
    call_count = 0

    def flaky_extract(*args: Any, **kwargs: Any) -> list[pdf_document.PDFDrawingLine]:
        """Only the first Path fails, subsequent objects still call the real implementation."""
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("broken path")
        return original_extract(*args, **kwargs)

    monkeypatch.setattr(native_objects, "_extract_path_drawing_lines", flaky_extract)
    with pdf_document.PDFDocument(_build_drawing_pdf()) as doc:
        lines = doc.get_page_drawing_lines(0)

    assert call_count >= 5
    assert len(lines) == 3
    assert all(line.start[1] != pytest.approx(20.0) for line in lines)
    assert [line.start[1] for line in lines] == pytest.approx([40.0, 60.25, 100.0])


def test_get_page_path_infos_skips_one_bad_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that failure to parse a single Path message will not lose other valid objects on the same page."""

    original_extract = pdf_document._path_info_from_object
    call_count = 0

    def flaky_extract(*args: Any, **kwargs: Any) -> pdf_document.PDFPathInfo | None:
        """Only the first Path fails, subsequent objects still call the real implementation."""

        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("broken path")
        return original_extract(*args, **kwargs)

    monkeypatch.setattr(native_objects, "_path_info_from_object", flaky_extract)
    with pdf_document.PDFDocument(_build_drawing_pdf()) as doc:
        path_infos = doc.get_page_path_infos(0)

    assert call_count >= 8
    assert len(path_infos) == 6
    assert all(item.source_index != 0 for item in path_infos)


@pytest.mark.parametrize(
    "builder",
    [
        _build_drawing_pdf,
        _build_rotated_cropped_drawing_pdf,
        _build_colored_path_pdf,
        _build_rotated_cropped_image_pdf,
        _build_rotated_cropped_signature_pdf,
        _build_rotated_cropped_link_pdf,
    ],
)
def test_native_page_snapshot_matches_independent_accessors(builder: Callable[[], bytes]) -> None:
    """Batch extraction is identical to the standalone interface for rotation, cropping, Form, signatures and linked corpora."""

    with pdf_document.PDFDocument(builder()) as document:
        snapshot = document._extract_native_page(0)
        assert snapshot.page_size == document.page_size(0)
        assert snapshot.rotation == document.page_rotation(0)
        assert snapshot.drawing_lines == document.get_page_drawing_lines(0)
        assert snapshot.path_infos == document.get_page_path_infos(0)
        assert snapshot.image_infos == document.get_page_image_infos(0)
        assert snapshot.form_bboxes == document.get_page_form_bboxes(0)
        assert snapshot.signature_bboxes == document.get_page_signature_bboxes(0)
        assert snapshot.link_annotations == document.get_page_link_annotations(0)
        geometry = document.get_page_chars_with_geometry(0)
        assert snapshot.text_geometry.tight_bboxes == geometry.tight_bboxes
        assert snapshot.text_geometry.loose_bboxes == geometry.loose_bboxes
        assert snapshot.text_geometry.origins == geometry.origins
        assert [(char["char"], char["char_idx"], tuple(char["bbox"])) for char in snapshot.text_geometry.chars] == [
            (char["char"], char["char_idx"], tuple(char["bbox"])) for char in geometry.chars
        ]


def test_native_page_snapshot_opens_once_and_closes_after_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Batch reading only opens the page once, and the native page is also closed when an error occurs in character extraction."""

    opened: list[pdf_document.pdfium.PdfPage] = []
    original = pdf_document.PDFDocument._open_page

    @contextmanager
    def record_page(document: pdf_document.PDFDocument, page_idx: int) -> Iterator[pdf_document.pdfium.PdfPage]:
        """Record the real page object and use the original life cycle management to check the closing status after success and failure."""

        with original(document, page_idx) as page:
            opened.append(page)
            yield page

    monkeypatch.setattr(pdf_document.PDFDocument, "_open_page", record_page)
    with pdf_document.PDFDocument(_build_drawing_pdf()) as document:
        document._extract_native_page(0)
        assert len(opened) == 1
        assert opened[0].raw is None

        def fail_text(
            page: pdf_document.pdfium.PdfPage, *, include_extended_geometry: bool, visible_only: bool = False
        ) -> pdf_document.PDFPageTextGeometry:
            """Simulated character extraction failed, verifying that the page life cycle is still managed by the outer context."""

            raise RuntimeError("broken text")

        monkeypatch.setattr(pdf_document, "_extract_page_text_geometry", fail_text)
        with pytest.raises(RuntimeError, match="broken text"):
            document._extract_native_page(0)
        assert len(opened) == 2
        assert opened[1].raw is None


def test_native_page_snapshot_decodes_each_path_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same Path decoding is shared by drawing line and path information, avoiding duplication of work by independent interfaces."""
    from docvortex.document.pdf import _object_bridge

    counts: list[object] = []
    original = pdf_document._read_raw_path_subpaths

    def record_decode(raw_object: Any) -> list[pdf_document._PathSubpath]:
        """Count the real Path decoding times and do not replace the decoding results."""

        counts.append(raw_object)
        return original(raw_object)

    monkeypatch.setattr(native_objects, "_read_raw_path_subpaths", record_decode)
    # This example verifies Python joint decoding; native batch paths and simple plot lines each have independent bridge tests.
    monkeypatch.setattr(_object_bridge, "read_path_evidence", lambda *args, **kwargs: None)
    monkeypatch.setattr(_object_bridge, "read_drawing_lines", lambda *args, **kwargs: None)
    with pdf_document.PDFDocument(_build_drawing_pdf()) as document:
        document.get_page_drawing_lines(0)
        document.get_page_path_infos(0)
        separate_count = len(counts)
        counts.clear()
        document._extract_native_page(0)
        shared_count = len(counts)
        with document._open_page(0) as page:
            expected_count = len(list(pdf_document._iter_raw_path_objects_with_depth(page)))
        assert shared_count == expected_count
        assert shared_count < separate_count
