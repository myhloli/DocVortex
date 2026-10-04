"""Check matrix, clipping, ordering and exception bounds for Rust object tree traversal."""

from contextlib import closing
import ctypes
from io import BytesIO
from unittest.mock import Mock

import pytest
import pypdfium2 as pdfium
import pypdfium2.raw as raw
from reportlab.pdfgen.canvas import Canvas

from docvortex._compute_backend import get_native
from docvortex.document.pdf import _object_bridge as bridge
from docvortex.document.pdf.native_objects import _text_object_visibility, _walk_clipped_objects
from docvortex.document.pdf.native_objects import _read_raw_path_subpaths, _read_raw_path_subpaths_python
from docvortex.document.pdf.pdfium import pdfium_guard


@pytest.fixture
def native():
    """Forced Rust tasks cannot be silently skipped, pure Python tasks remain in reference test mode."""
    extension = get_native()
    if extension is None:
        pytest.skip("native backend is not selected")
    return extension


def nested_pdf():
    """Build a real PDF with nested Forms, rotations, bevels, curve clipping and invisible areas."""
    stream = BytesIO()
    canvas = Canvas(stream)
    canvas.beginForm("inner")
    canvas.drawString(10, 20, "Nested text")
    canvas.rect(0, 0, 60, 60)
    canvas.endForm()
    canvas.beginForm("outer")
    canvas.translate(40, 30)
    canvas.rotate(15)
    path = canvas.beginPath()
    path.moveTo(0, 0)
    path.curveTo(80, 0, 80, 80, 0, 80)
    path.close()
    canvas.clipPath(path, stroke=0)
    canvas.doForm("inner")
    canvas.endForm()
    canvas.translate(100, 100)
    canvas.skew(5, -10)
    canvas.doForm("outer")
    canvas.doForm("inner")
    canvas.save()
    return stream.getvalue()


@pytest.mark.parametrize("kind", [raw.FPDF_PAGEOBJ_TEXT, raw.FPDF_PAGEOBJ_PATH, raw.FPDF_PAGEOBJ_IMAGE])
def test_object_bridge_matches_reference(native, kind):
    """The same page compares the borrowed object address, matrix, clipping and traversal order field by field."""
    with pdfium_guard(), pdfium.PdfDocument(nested_pdf()) as document:
        with closing(document[0]) as page:
            expected = [
                (ctypes.cast(item.raw, ctypes.c_void_p).value, item.matrix, item.parent_matrix, item.depth, item.clip)
                for item in _walk_clipped_objects(page)
                if raw.FPDFPageObj_GetType(item.raw) == kind
            ]
            assert list(bridge.read_clipped_objects(page, kind, 15)) == expected


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_text_visibility_bridge_matches_reference(native, monkeypatch, rotation):
    """Rust reads TEXT status/cropping in the same batch, and is consistent with Python attribute reading field by field."""
    with pdfium_guard(), pdfium.PdfDocument(nested_pdf()) as document:
        with closing(document[0]) as page:
            page.set_rotation(rotation)
            before = bridge.bridge_info()["pdfium_text_visibility_bridge_calls"]
            actual = bridge.read_text_visibility(page, page.get_bbox(), rotation, 15)
            info = bridge.bridge_info()
            assert actual is not None
            assert info["pdfium_text_visibility_bridge_calls"] == before + 1
            assert info["pdfium_text_visibility_bridge_unavailable_reason"] is None
            with monkeypatch.context() as context:
                context.setattr(bridge, "read_text_visibility", lambda *args, **kwargs: None)
                expected = _text_object_visibility(page, page.get_bbox(), rotation)
            assert {address: (visible, clip) for address, visible, clip in actual} == expected
            assert any(visible for _, visible, _ in actual)
            assert any(clip is not None for _, _, clip in actual)


def test_text_visibility_bridge_fallback_and_compute_failure(native, monkeypatch):
    """ABI is not compatible with fallback Python; exceptions that have entered Rust must be propagated unchanged."""
    assert hasattr(native, "read_pdfium_text_visibility")
    with pdfium_guard(), pdfium.PdfDocument(nested_pdf()) as document:
        with closing(document[0]) as page:
            bbox, rotation = page.get_bbox(), 0
            with monkeypatch.context() as context:
                context.setattr(raw, "FPDFTextObj_GetTextRenderMode", Mock())
                assert bridge.read_text_visibility(page, bbox, rotation, 15) is None
                assert "ABI" in bridge.bridge_info()["pdfium_text_visibility_bridge_unavailable_reason"]
            with monkeypatch.context() as context:
                context.setattr(native, "read_pdfium_text_visibility", Mock(side_effect=ValueError("failed")))
                with pytest.raises(ValueError, match="failed"):
                    bridge.read_text_visibility(page, bbox, rotation, 15)


def test_text_visibility_python_backend_reports_reference_choice(monkeypatch):
    """The pure Python backend only records capability selections and does not disguise the absence of extensions as successful execution."""

    before = bridge.bridge_info()["pdfium_text_visibility_bridge_calls"]
    monkeypatch.setattr(bridge, "get_native", lambda: None)
    assert bridge.read_text_visibility(object(), (0, 0, 10, 10), 0, 15) is None
    info = bridge.bridge_info()
    assert info["pdfium_text_visibility_bridge_calls"] == before
    assert info["pdfium_text_visibility_bridge_unavailable_reason"] == "python backend or unsupported native extension"


def test_text_visibility_bridge_rejects_invalid_addresses(native):
    """Invalid addresses or geometries are rejected before unsafe to prevent borrowing of dangling page objects."""
    for addresses, handle, rotation, frame in [
        ([], 1, 0, (0.0, 0.0, 10.0, 10.0)),
        ([0] * 14, 1, 0, (0.0, 0.0, 10.0, 10.0)),
        ([1] * 14, 0, 0, (0.0, 0.0, 10.0, 10.0)),
        ([1] * 14, 1, 45, (0.0, 0.0, 10.0, 10.0)),
        ([1] * 14, 1, 0, (0.0, 0.0, 0.0, 10.0)),
    ]:
        with pytest.raises(ValueError, match="invalid PDFium text visibility"):
            native.read_pdfium_text_visibility(addresses, handle, frame, rotation, 15)


def test_object_bridge_abi_fallback_and_compute_failure(native, monkeypatch):
    """Only capability incompatibilities allow reference fallback, exceptions that have entered Rust must be propagated."""
    with pdfium_guard(), pdfium.PdfDocument(nested_pdf()) as document:
        with closing(document[0]) as page:
            with monkeypatch.context() as context:
                context.setattr(raw, "FPDFPageObj_GetMatrix", Mock())
                assert bridge.read_clipped_objects(page, raw.FPDF_PAGEOBJ_TEXT, 15) is None
            with monkeypatch.context() as context:
                context.setattr(native, "read_pdfium_objects", Mock(side_effect=ValueError("failed")))
                with pytest.raises(ValueError, match="failed"):
                    bridge.read_clipped_objects(page, raw.FPDF_PAGEOBJ_TEXT, 15)


def test_object_bridge_rejects_invalid_addresses(native):
    """Invalid addresses are rejected before entering the unsafe call without triggering a native crash."""
    for addresses, handle, depth in [([], 1, 15), ([0] * 11, 1, 15), ([1] * 11, 0, 15), ([1] * 11, 1, 65)]:
        with pytest.raises(ValueError, match="invalid PDFium"):
            native.read_pdfium_objects(addresses, handle, 1, depth)


def test_drawing_bridge_fallback_and_compute_failure(native, monkeypatch):
    """Complex Form and ABI are incompatible fallbacks; calculation errors after entering Rust are propagated unchanged."""
    assert hasattr(native, "read_pdfium_drawing_lines")
    with pdfium_guard(), pdfium.PdfDocument(nested_pdf()) as document:
        with closing(document[0]) as page:
            bbox = page.get_bbox()
            assert bridge.read_drawing_lines(page, bbox, 0) is None
            assert "complex" in bridge.bridge_info()["pdfium_drawing_line_bridge_unavailable_reason"]
            with monkeypatch.context() as context:
                context.setattr(raw, "FPDFPageObj_GetStrokeWidth", Mock())
                assert bridge.read_drawing_lines(page, bbox, 0) is None
                assert "ABI" in bridge.bridge_info()["pdfium_drawing_line_bridge_unavailable_reason"]
            with monkeypatch.context() as context:
                context.setattr(native, "read_pdfium_drawing_lines", Mock(side_effect=ValueError("failed")))
                with pytest.raises(ValueError, match="failed"):
                    bridge.read_drawing_lines(page, bbox, 0)


def test_native_subpaths_match_reference_and_share_endpoints(native):
    """Native decoding preserves references to subpaths, curve control points, closed edges, and same-point objects."""
    stream = BytesIO()
    canvas = Canvas(stream)
    path = canvas.beginPath()
    path.moveTo(10, 10)
    path.lineTo(50, 50)
    path.curveTo(70, 20, 80, 30, 50, 70)
    path.close()
    path.moveTo(100, 100)
    path.lineTo(120, 110)
    canvas.drawPath(path)
    canvas.save()
    with pdfium_guard(), pdfium.PdfDocument(stream.getvalue()) as document:
        with closing(document[0]) as page:
            paths = [
                item.raw for item in _walk_clipped_objects(page) if raw.FPDFPageObj_GetType(item.raw) == raw.FPDF_PAGEOBJ_PATH
            ]
            assert paths
            for path in paths:
                expected = _read_raw_path_subpaths_python(path)
                actual = _read_raw_path_subpaths(path)
                assert actual == expected
                for subpath in actual:
                    for first, last in subpath.straight_segments:
                        assert any(first is point for point in subpath.points)
                        assert any(last is point for point in subpath.points)
