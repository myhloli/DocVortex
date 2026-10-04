"""Check the original characters and life cycle of the same library PDFium batch read with the ctypes reference."""

from io import BytesIO
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock
import ctypes
from concurrent.futures import ThreadPoolExecutor
from collections import UserDict

import pytest
import pypdfium2 as pdfium
from reportlab.pdfgen.canvas import Canvas

from docvortex._compute_backend import get_native
from docvortex.document.pdf.pdfium import pdfium_guard
from docvortex.document.pdf.text import extract
from docvortex.document.pdf.text import _pdfium_bridge as bridge


@pytest.fixture
def native():
    """Forced native environment to explicitly fail when missing extensions, pure Python test task allows to skip bridging."""
    value = get_native()
    if value is None:
        pytest.skip("native backend is not selected")
    return value


def character_state(chars):
    """Compares all original field and font alias relationships without relying on the object address of the rectangle instance."""
    fonts = {}
    return [
        (
            {key: value.bbox if key == "bbox" else value for key, value in char.items()},
            fonts.setdefault(id(char["font"]), len(fonts)),
        )
        for char in chars
    ]


def sample_pdf(rotation):
    """Construct visible, hidden and repainted text, and cover page rotation and blank pages."""
    stream = BytesIO()
    canvas = Canvas(stream)
    canvas.setPageRotation(rotation)
    canvas.drawString(50, 100, "Repeated text 123")
    canvas.drawString(50.2, 100.2, "Repeated text 123")
    text = canvas.beginText(50, 100)
    text.setTextRenderMode(3)
    text.textOut("Repeated text 123")
    canvas.drawText(text)
    canvas.showPage()
    canvas.showPage()
    canvas.save()
    return stream.getvalue()


@pytest.mark.parametrize("rotation", (0, 90, 180, 270))
@pytest.mark.parametrize("extended", (False, True))
def test_raw_character_bridge_parity(native, rotation, extended):
    """Checked field by field on the same textpage, font grouping, hidden text and rotation results must not change."""
    with pdfium_guard(), pdfium.PdfDocument(sample_pdf(rotation)) as document:
        for page_index in range(len(document)):
            with closing(document[page_index]) as page, closing(page.get_textpage()) as textpage:
                before = bridge.bridge_info()
                count = textpage.count_chars()
                expected = extract._get_chars_python(
                    textpage, list(page.get_bbox()), page.get_rotation(), include_geometry=extended
                )
                actual = extract.get_chars(textpage, list(page.get_bbox()), page.get_rotation(), include_geometry=extended)
                after = bridge.bridge_info()
                assert after["pdfium_bridge_calls"] == before["pdfium_bridge_calls"] + int(count > 0)
                assert after["pdfium_empty_pages"] == before["pdfium_empty_pages"] + int(count == 0)
                assert character_state(actual) == character_state(expected)


@pytest.mark.parametrize("name", ("caibao1", "demo1", "demo2"))
def test_real_font_and_geometry_bridge_parity(native, name):
    """Real Chinese fonts and math characters are already identical before decoding, surrogate pair recovery, and deduplication."""
    path = Path(__file__).parents[1] / "demo/pdfs" / f"{name}.pdf"
    with pdfium_guard(), pdfium.PdfDocument(path) as document:
        with closing(document[0]) as page, closing(page.get_textpage()) as textpage:
            box = list(page.get_bbox())
            expected = extract._get_chars_python(textpage, box, page.get_rotation(), include_geometry=True)
            actual = extract.get_chars(textpage, box, page.get_rotation(), include_geometry=True)
            assert character_state(actual) == character_state(expected)


def test_bridge_unavailable_and_error_are_distinct(native, monkeypatch):
    """There is a clear fallback for lack of capabilities, and errors that have entered native calculations are propagated directly without re-reading the page."""
    with pdfium_guard(), pdfium.PdfDocument(sample_pdf(0)) as document:
        with closing(document[0]) as page, closing(page.get_textpage()) as textpage:
            with monkeypatch.context() as context:
                context.setattr(bridge.raw, "FPDFText_GetFontInfo", Mock())
                assert bridge.read_native_chars(textpage, True) is None
                assert "FPDFText_GetFontInfo" in bridge.bridge_info()["pdfium_bridge_unavailable_reason"]
            with monkeypatch.context() as context:
                reader_name = "read_pdfium_char_batches" if hasattr(native, "read_pdfium_char_batches") else "read_pdfium_chars"
                context.setattr(native, reader_name, Mock(side_effect=ValueError("calculation failure")))
                with pytest.raises(ValueError, match="calculation failure"):
                    bridge.read_native_chars(textpage, True)
        assert bridge.read_native_chars(textpage, True) is None


def test_bridge_rejects_null_arguments_before_ffi(native):
    """A null handle or null function address is not allowed into a unsafe call."""
    with pytest.raises(ValueError, match="invalid PDFium"):
        native.read_pdfium_chars([0] * 10, 1, 1, True)
    with pytest.raises(ValueError, match="invalid PDFium"):
        native.read_pdfium_chars([1] * 10, 0, 1, True)
    with pytest.raises(ValueError, match="invalid PDFium"):
        native.read_pdfium_char_batches([0] * 10, 1, 1, True)


def test_batched_records_preserve_boundary_indices_and_legacy_reader(native, monkeypatch):
    """Proxy pairs and source ordinal numbers across materialized batches remain unchanged, and old list reads from protocol 4 remain compatible."""
    stream = BytesIO()
    canvas = Canvas(stream)
    for row in range(24):
        canvas.drawString(30, 780 - row * 14, "boundary 1234567890 " * 3)
    canvas.save()
    factory = ctypes.WINFUNCTYPE if hasattr(ctypes, "WINFUNCTYPE") else ctypes.CFUNCTYPE
    unicode_reader = bridge.raw.FPDFText_GetUnicode

    def unicode_value(handle, index):
        """Place pairwise surrogates at numeric batch boundaries and check that full-page indexes are not reset on new batches."""
        return {1023: 0xD83D, 1024: 0xDE00}.get(index, unicode_reader(handle, index))

    monkeypatch.setattr(
        bridge.raw, "FPDFText_GetUnicode", factory(unicode_reader.restype, *unicode_reader.argtypes)(unicode_value)
    )
    with pdfium_guard(), pdfium.PdfDocument(stream.getvalue()) as document:
        with closing(document[0]) as page, closing(page.get_textpage()) as textpage:
            assert textpage.count_chars() > 1024
            box = list(page.get_bbox())
            expected = character_state(extract._get_chars_python(textpage, box, 0, include_geometry=True))
            assert character_state(extract.get_chars(textpage, box, 0, include_geometry=True)) == expected
            assert bridge.bridge_info()["pdfium_record_batch_size"] == 1024
            with monkeypatch.context() as context:
                context.delattr(native, "read_pdfium_char_batches")
                assert character_state(extract.get_chars(textpage, box, 0, include_geometry=True)) == expected
                # The new fusion portal uses bounded batches independently; the old raw read portal can still return the full list.
                records, _fonts = bridge.read_native_chars(textpage, True)
                assert len(records) == textpage.count_chars()
                assert bridge.bridge_info()["pdfium_record_batch_size"] is None


def test_bridge_long_font_callback_and_surrogates(native, monkeypatch):
    """The real handle reuses the same ctypes callback, covering long font accents, illegal UTF8 and proxy code values."""
    factory = ctypes.WINFUNCTYPE if hasattr(ctypes, "WINFUNCTYPE") else ctypes.CFUNCTYPE
    font_reader = bridge.raw.FPDFText_GetFontInfo
    unicode_reader = bridge.raw.FPDFText_GetUnicode
    font_name = b"Synthetic-" + b"A" * 300 + b"\xff\0"

    def font_info(handle, index, buffer, capacity, flags):
        """Fill the buffer to the PDFium length protocol to ensure the second read path actually executes."""
        flags[0] = 32
        if capacity >= len(font_name):
            ctypes.memmove(buffer, font_name, len(font_name))
        return len(font_name)

    def unicode_value(handle, index):
        """Inject high and low surrogates and missing glyph markers respectively, and use the actual PDFium for other code values."""
        return {0: 0xD83D, 1: 0xDE00, 2: 0}.get(index, unicode_reader(handle, index))

    monkeypatch.setattr(bridge.raw, "FPDFText_GetFontInfo", factory(font_reader.restype, *font_reader.argtypes)(font_info))
    monkeypatch.setattr(
        bridge.raw, "FPDFText_GetUnicode", factory(unicode_reader.restype, *unicode_reader.argtypes)(unicode_value)
    )
    with pdfium_guard(), pdfium.PdfDocument(sample_pdf(0)) as document:
        with closing(document[0]) as page, closing(page.get_textpage()) as textpage:
            box = list(page.get_bbox())
            expected = extract._get_chars_python(textpage, box, 0, include_geometry=True)
            before = bridge.bridge_info()["pdfium_bridge_calls"]
            actual = extract.get_chars(textpage, box, 0, include_geometry=True)
            assert bridge.bridge_info()["pdfium_bridge_calls"] == before + 1
            assert character_state(actual) == character_state(expected)
            assert any("\ufffd" in char["font"]["name"] for char in actual)


def test_bridge_charbox_failure_propagates(native, monkeypatch):
    """PDFium read failure that has entered the bridge remains with the same exception, without silently retrying or returning half a page of characters."""
    factory = ctypes.WINFUNCTYPE if hasattr(ctypes, "WINFUNCTYPE") else ctypes.CFUNCTYPE
    reader = bridge.raw.FPDFText_GetLooseCharBox

    def failed_box(handle, index, rectangle):
        """Simulates the PDFium failure return value under the legal ABI."""
        return 0

    monkeypatch.setattr(bridge.raw, "FPDFText_GetLooseCharBox", factory(reader.restype, *reader.argtypes)(failed_box))
    with pdfium_guard(), pdfium.PdfDocument(sample_pdf(0)) as document:
        with closing(document[0]) as page, closing(page.get_textpage()) as textpage:
            for reader in (extract._get_chars_python, extract.get_chars):
                with pytest.raises(pdfium.PdfiumError, match="Failed to get charbox"):
                    reader(textpage, list(page.get_bbox()), 0, include_geometry=True)


def test_bridge_concurrent_requests_keep_existing_guard(native):
    """Multiple requests read serially using the original reentrant lock, with each request independently owning and closing the handle."""
    payload = sample_pdf(0)

    def capture(_index):
        """Opening, two-path reading, and closing are done within the lock, and independent comparable data is returned."""
        with pdfium_guard(), pdfium.PdfDocument(payload) as document:
            with closing(document[0]) as page, closing(page.get_textpage()) as textpage:
                box = list(page.get_bbox())
                expected = extract._get_chars_python(textpage, box, 0, include_geometry=True)
                actual = extract.get_chars(textpage, box, 0, include_geometry=True)
                assert character_state(actual) == character_state(expected)
                return character_state(actual)

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(capture, range(6)))
    assert all(result == results[0] for result in results)


def test_special_visibility_mapping_preserves_reference_access(native, monkeypatch):
    """Custom mapping and non-conventional page frames maintain the original reading entry to avoid the side effects of early batch reading changes to Python access."""
    blocked = Mock(side_effect=AssertionError("special input entered native reader"))
    monkeypatch.setattr(extract, "_get_chars_native", blocked)
    with pdfium_guard(), pdfium.PdfDocument(sample_pdf(0)) as document:
        with closing(document[0]) as page, closing(page.get_textpage()) as textpage:
            box = list(page.get_bbox())
            visibility = UserDict()
            expected = extract._get_chars_python(textpage, box, 0, include_geometry=True, visibility_by_object=visibility)
            actual = extract.get_chars(textpage, box, 0, include_geometry=True, visibility_by_object=visibility)
            assert character_state(actual) == character_state(expected)
            integer_box = [int(value) for value in box]
            expected = extract._get_chars_python(textpage, integer_box, 0, include_geometry=True)
            assert character_state(extract.get_chars(textpage, integer_box, 0, include_geometry=True)) == character_state(
                expected
            )
    blocked.assert_not_called()


def test_empty_page_skips_character_ffi(native, monkeypatch):
    """Empty pages do not pack character function addresses, nor do they falsely report empty snapshots as actual Rust reads."""

    def forbidden(*args):
        """If an empty page enters the character read or coordinate kernel, it will explicitly fail."""
        raise AssertionError("empty page entered character kernel")

    monkeypatch.setattr(native, "read_pdfium_char_batches", forbidden)
    monkeypatch.setattr(native, "read_pdfium_chars", forbidden)
    monkeypatch.setattr(native, "materialize_geometry", forbidden)
    with pdfium_guard(), pdfium.PdfDocument(sample_pdf(0)) as document:
        with closing(document[1]) as page, closing(page.get_textpage()) as textpage:
            before = bridge.bridge_info()
            assert extract.get_chars(textpage, list(page.get_bbox()), 0, include_geometry=True) == []
            after = bridge.bridge_info()
            assert after["pdfium_bridge_calls"] == before["pdfium_bridge_calls"]
            assert after["pdfium_empty_pages"] == before["pdfium_empty_pages"] + 1
