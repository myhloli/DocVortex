"""Verify Office integration of the standalone WMF/EMF rendering package."""

from __future__ import annotations

import base64
import struct
import zlib
from io import BytesIO
from unittest.mock import Mock
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from _legacy_ppt_test_utils import build_equation_ppt
from _legacy_xls_test_utils import build_equation_xls
from _metafile_test_utils import (
    basic_wmf,
    build_emf,
    emf_create_brush,
    emf_create_pen,
    emf_font,
    emf_rectangle,
    emf_select_object,
    emf_stretch_dib,
    emf_text,
)
from _mtef_test_utils import build_equation_doc
from _odf_test_utils import build_odp_fixture, build_ods_fixture, build_odt_fixture
from _office_image_mtef_test_utils import build_image_docx, build_image_pptx, build_image_xlsx
from metafile_render import (
    MetafileResourceLimitError,
    render_metafile,
)
from PIL import Image

from docvortex.analyzers.native import (
    DocModel,
    DocxModel,
    OdpModel,
    OdsModel,
    OdtModel,
    PptModel,
    PptxModel,
    RtfModel,
    XlsModel,
    XlsxModel,
)
from docvortex.analyzers.native.office import image as office_image
from docvortex.analyzers.native.office.legacy.officeart import OfficeArtRecord, decode_blip
from docvortex.analyzers.native.office.pptx.pptx_converter import PptxConverter
from docvortex.analyzers.native.office.xlsx.xlsx_converter import XlsxConverter
from docvortex.foundation._image_payload import extract_generated_svg_fallback


def _open_result(payload: bytes) -> Image.Image:
    """Decode the rendering result and return the RGBA image out of the BytesIO life cycle."""
    with Image.open(BytesIO(payload)) as image:
        image.load()
        return image.convert("RGBA")


def _svg_data_uri_fallback(data_uri: str) -> tuple[Image.Image, tuple[int, int], bytes]:
    """Parses MinerU SVG data URI and returns the fallback picture, logical dimensions, and SVG."""
    assert data_uri.startswith("data:image/svg+xml;base64,")
    svg = base64.b64decode(data_uri.split(",", 1)[1])
    fallback, logical_width, logical_height = extract_generated_svg_fallback(svg)
    return _open_result(fallback), (logical_width, logical_height), svg


def _basic_emf_records() -> list[bytes]:
    """Returns base EMF records covering brushes, brushes, text, and DIB."""
    return [
        emf_create_pen(1, 0x00FF0000, width=2),
        emf_create_brush(2, 0x0000FF00),
        emf_select_object(1),
        emf_select_object(2),
        emf_rectangle(5, 5, 95, 95),
        emf_stretch_dib(),
        emf_font(3),
        emf_select_object(3),
        emf_text("EMF", 20, 50, dx=18),
    ]


def _collect_image_data_uris(value: object) -> list[str]:
    """Recursively collect the image_base64 fields in raw model-list."""
    if isinstance(value, dict):
        result = [value["image_base64"]] if isinstance(value.get("image_base64"), str) else []
        for item in value.values():
            result.extend(_collect_image_data_uris(item))
        return result
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_collect_image_data_uris(item))
        return result
    return []


def _replace_zip_member(package: bytes, member_name: str, payload: bytes) -> bytes:
    """Replace a single member in test ZIP and preserve other members and compression."""
    output = BytesIO()
    with ZipFile(BytesIO(package)) as source, ZipFile(output, "w") as target:
        for info in source.infolist():
            target.writestr(info, payload if info.filename == member_name else source.read(info.filename))
    return output.getvalue()


def _build_doc_with_wmf_preview(payload: bytes) -> bytes:
    """DOC previewed using WMF PICF after failure to construct Native."""
    return build_equation_doc(
        [(1, b"invalid")],
        preview_storage_ids={1},
        preview_payloads={1: payload},
    )


def _build_ppt_with_wmf_preview(payload: bytes) -> bytes:
    """PPT previewed using OfficeArt WMF after failure to construct Native."""
    return build_equation_ppt([b"invalid"], preview_payload=payload)


def _build_xls_with_wmf_preview(payload: bytes) -> bytes:
    """XLS previewed using OfficeArt WMF after failure to construct Native."""
    return build_equation_xls([(1, b"invalid")], preview_payload=payload)


def test_oversized_generated_svg_degrades_to_png_for_office_consumers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verification that Office consumers reliably fall back to PNG by exposing a resource error for API."""
    data = build_emf([emf_stretch_dib()])
    png = render_metafile(data, output_format="png")
    render = Mock(side_effect=[MetafileResourceLimitError("generated SVG exceeds budget"), png])
    monkeypatch.setattr(office_image, "render_metafile", render)
    data_uri = office_image.serialize_office_image(data, part_name="image.emf", content_type="image/emf")
    assert data_uri is not None
    assert data_uri.startswith("data:image/png;base64,")
    assert [call.kwargs["output_format"] for call in render.call_args_list] == ["svg", "png"]
    assert all(call.kwargs["dpi"] == 200 and call.kwargs["backend"] == "auto" for call in render.call_args_list)


def test_office_serializer_returns_generated_svg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that the Office image portal returns security SVG and PNG fallback."""
    data_uri = office_image.serialize_office_image(
        build_emf(_basic_emf_records()),
        part_name="/word/media/image1.emf",
        content_type="image/x-emf",
    )

    assert data_uri is not None
    fallback, logical_size, svg = _svg_data_uri_fallback(data_uri)
    assert logical_size == (200, 200)
    assert fallback.getbbox() is not None
    assert b"<path" in svg


def test_officeart_emu_size_controls_standard_wmf_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that OfficeArt without placeable header WMF is calibrated using ptSize EMU."""
    standard_wmf = basic_wmf()[22:]
    data_uri = office_image.serialize_office_image(
        standard_wmf,
        part_name="picture.wmf",
        content_type="image/wmf",
        render_size_emu=(914_400, 457_200),
    )

    assert data_uri is not None
    fallback, logical_size, _svg = _svg_data_uri_fallback(data_uri)
    assert logical_size == (200, 100)
    assert fallback.size == (400, 200)


def test_officeart_metafile_header_preserves_payload_and_emu_size() -> None:
    """Verify BLIP unpack retains WMF bytes, and read specifications ptSize EMU."""
    standard_wmf = basic_wmf()[22:]
    compressed = zlib.compress(standard_wmf)
    metafile_header = bytearray(34)
    struct.pack_into("<I", metafile_header, 0, len(standard_wmf))
    struct.pack_into("<ii", metafile_header, 20, 914_400, 457_200)
    struct.pack_into("<I", metafile_header, 28, len(compressed))
    metafile_header[32:34] = b"\x00\xfe"
    record = OfficeArtRecord(
        offset=0,
        version=0,
        instance=0,
        record_type=0xF01B,
        payload=b"\x00" * 16 + bytes(metafile_header) + compressed,
    )

    decoded = decode_blip(record)

    assert decoded is not None
    assert decoded.data == standard_wmf
    assert decoded.render_size_emu == (914_400, 457_200)


def test_office_serializer_uses_only_library_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """All platforms only call independent libraries, and use placeholder images directly after failure."""
    data = build_emf(_basic_emf_records())
    generated = Mock(return_value="data:image/svg+xml;base64,generated")
    pillow_open = Mock(side_effect=AssertionError("MinerU must not render metafiles with Pillow"))
    monkeypatch.setattr(office_image, "_serialize_metafile", generated)
    monkeypatch.setattr(office_image.Image, "open", pillow_open)
    monkeypatch.setattr(office_image, "get_standard_vector_placeholder_data_uri", lambda: "placeholder")
    assert office_image.serialize_office_image(data) == "data:image/svg+xml;base64,generated"
    generated.return_value = None
    assert office_image.serialize_office_image(data) == "placeholder"
    pillow_open.assert_not_called()


def test_pptx_picture_path_uses_cross_platform_metafile_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verification PPTX Common picture no longer bypasses the shared WMF/EMF serialization entry."""
    converter = PptxConverter()
    data = build_emf(_basic_emf_records())

    def fake_image_data(_shape: object) -> tuple[bytes, str]:
        """Return fixed EMF payload to isolate shape relationship parsing."""
        return data, "image/x-emf"

    monkeypatch.setattr(converter, "_get_shape_image_data", fake_image_data)
    converter._handle_pictures(object())

    assert converter.cur_page[0]["image_base64"].startswith("data:image/svg+xml;base64,")


def test_xlsx_wps_cell_image_uses_cross_platform_metafile_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify WPS The cell-image branch of DISPIMG uniformly calls the Office image serializer."""
    package = BytesIO()
    with ZipFile(package, "w", ZIP_DEFLATED) as archive:
        archive.writestr("xl/media/image1.emf", build_emf(_basic_emf_records()))
    converter = XlsxConverter()
    converter.zf = ZipFile(BytesIO(package.getvalue()))
    converter.cell_image_map = {"image-id": "media/image1.emf"}
    try:
        html = converter._resolve_cell_image('DISPIMG("image-id")')
    finally:
        converter.zf.close()

    assert html.startswith('<img src="data:image/svg+xml;base64,')


@pytest.mark.parametrize(
    ("model", "builder"),
    [
        (DocxModel(), build_image_docx),
        (PptxModel(), build_image_pptx),
        (XlsxModel(), build_image_xlsx),
    ],
    ids=["docx", "pptx", "xlsx"],
)
def test_modern_office_models_emit_rendered_emf_svg(
    model: object,
    builder: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that the complete model chain of DOCX/PPTX/XLSX yields safe EMF and SVG."""
    package = builder(build_emf(_basic_emf_records()))  # type: ignore[operator]
    pages = model.predict(BytesIO(package))  # type: ignore[attr-defined]
    images = _collect_image_data_uris(pages)

    assert images
    assert all(image.startswith("data:image/svg+xml;base64,") for image in images)
    _fallback, logical_size, _svg = _svg_data_uri_fallback(images[0])
    assert logical_size == (200, 200)


@pytest.mark.parametrize(
    ("model", "builder"),
    [
        (DocModel(), _build_doc_with_wmf_preview),
        (PptModel(), _build_ppt_with_wmf_preview),
        (XlsModel(), _build_xls_with_wmf_preview),
    ],
    ids=["doc", "ppt", "xls"],
)
def test_legacy_office_models_emit_rendered_wmf_svg(
    model: object,
    builder: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verified OfficeArt/PICF WMF preview of DOC/PPT/XLS into cross-platform SVG."""
    pages = model.predict(BytesIO(builder(basic_wmf())))  # type: ignore[attr-defined,operator]
    images = _collect_image_data_uris(pages)

    assert images
    assert all(image.startswith("data:image/svg+xml;base64,") for image in images)
    fallback, logical_size, _svg = _svg_data_uri_fallback(images[0])
    assert logical_size == (200, 200)
    assert fallback.size == (400, 400)


def test_rtf_model_emf_picture_emits_rendered_svg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verification RTF emfblip captures from pict all the way into the cross-platform SVG."""
    emf = build_emf(_basic_emf_records())
    rtf = b"{\\rtf1 before {\\pict\\emfblip " + emf.hex().encode("ascii") + b"} after\\par}"
    images = _collect_image_data_uris(RtfModel().predict(BytesIO(rtf)))

    assert images
    assert all(image.startswith("data:image/svg+xml;base64,") for image in images)


@pytest.mark.parametrize(
    ("model", "builder"),
    [
        (OdtModel(), build_odt_fixture),
        (OdsModel(), build_ods_fixture),
        (OdpModel(), build_odp_fixture),
    ],
    ids=["odt", "ods", "odp"],
)
def test_odf_models_emit_rendered_emf_svg(
    model: object,
    builder: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that all WMF/EMF images in the ODT/ODS/ODP package reuse the unified rendering entry."""
    package = builder()  # type: ignore[operator]
    package = _replace_zip_member(package, "Pictures/pixel.png", build_emf(_basic_emf_records()))
    images = _collect_image_data_uris(model.predict(BytesIO(package)))  # type: ignore[attr-defined]

    assert images
    assert all(image.startswith("data:image/svg+xml;base64,") for image in images)


def test_pillow_identified_metafile_is_routed_before_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """The vector images supplemented by Pillow are also handed over to the library for processing, which prohibits triggering the internal native loading of MinerU."""
    image = Mock(format="WMF")
    generated = Mock(return_value="data:image/svg+xml;base64,generated")
    monkeypatch.setattr(office_image.Image, "open", Mock(return_value=image))
    monkeypatch.setattr(office_image, "_serialize_metafile", generated)
    data = b"identified-by-pillow"
    result = office_image.serialize_office_image(data, render_size_emu=(914400, 457200))
    assert result == "data:image/svg+xml;base64,generated"
    generated.assert_called_once_with(data, "WMF", size_hint=(200, 100))
    image.load.assert_not_called()
