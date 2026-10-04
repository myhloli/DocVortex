"""Explicit page snapshot lifecycle, provenance verification, and shared evidence regression."""

from dataclasses import FrozenInstanceError
from io import BytesIO
from unittest.mock import patch
import gc
import pickle
import weakref

import pytest
from reportlab.pdfgen.canvas import Canvas

from docvortex.analyzers.pdf import prepare_table_page, prepare_text_evidence
from docvortex.document.pdf import PDFDocument


def pdf_bytes():
    """Generates a two-page PDF containing text, links and paths to avoid relying on external samples."""
    stream = BytesIO()
    canvas = Canvas(stream, pagesize=(400, 500))
    for index in range(2):
        canvas.drawString(30, 430, f"Snapshot page {index}")
        canvas.linkURL("https://example.com", (30, 420, 150, 445))
        canvas.rect(20, 400, 180, 60)
        canvas.showPage()
    canvas.save()
    return stream.getvalue()


def test_snapshot_survives_close_and_isolates_mutations():
    """The snapshot is separated from the native resource, and the caller's modification of the exported characters will not affect subsequent reading."""
    with PDFDocument(pdf_bytes()) as document:
        page = document[0]
        expected = pickle.dumps(page.get_chars_with_geometry())
        snapshot = page.get_snapshot()
        assert page.get_snapshot() is snapshot
        assert snapshot.vector_geometry == page.get_vector_geometry()
        assert snapshot.link_annotations == tuple(page.get_link_annotations())
    geometry = snapshot.text_geometry
    assert pickle.dumps(geometry) == expected
    geometry.chars[0]["font"]["name"] = "changed"
    geometry.chars[0]["bbox"].bbox[0] = -999
    geometry.chars.clear()
    assert pickle.dumps(snapshot.text_geometry) == expected
    with pytest.raises(FrozenInstanceError):
        snapshot.rotation = 90


def test_snapshot_consumers_do_not_reopen_pdf():
    """Characters, paths, links, dimensions and rotations are all taken from the snapshot and continue to generate the same evidence after closing."""
    document = PDFDocument(pdf_bytes())
    page = document[0]
    expected = prepare_text_evidence(page)
    expected_table = prepare_table_page(page)
    snapshot = page.get_snapshot()
    document.close()
    with patch.object(document, "_open_page", side_effect=AssertionError("reopened")):
        actual = prepare_text_evidence(page, snapshot=snapshot)
        table = prepare_table_page(page, snapshot=snapshot)
    assert pickle.dumps(actual) == pickle.dumps(expected)
    assert pickle.dumps(table) == pickle.dumps(expected_table)


def test_snapshot_rejects_wrong_page_and_mixed_geometry():
    """Reject cross-page, cross-document, and explicit geometry mixes instead of silently accepting evidence of error."""
    with PDFDocument(pdf_bytes()) as first, PDFDocument(pdf_bytes()) as second:
        snapshot = first[0].get_snapshot()
        for prepare in (prepare_text_evidence, prepare_table_page):
            for page in (first[1], second[0]):
                with pytest.raises(ValueError, match="different PDF page"):
                    prepare(page, snapshot=snapshot)
            with pytest.raises(ValueError, match="explicit geometry"):
                prepare(first[0], snapshot=snapshot, geometry=snapshot.text_geometry)


def test_snapshot_cache_does_not_retain_consumed_pages():
    """The weak cache allows page evidence recovery immediately after the consumer is released, without forming a document-level strong reference."""
    with PDFDocument(pdf_bytes()) as document:
        snapshot = document[0].get_snapshot()
        reference = weakref.ref(snapshot)
        del snapshot
        gc.collect()
        assert reference() is None
        assert not document._page_snapshots


def test_snapshot_extracts_once_and_failure_is_not_cached():
    """The first failure does not leave a semi-snapshot, and the same page call only opens the page once after successful extraction."""
    with PDFDocument(pdf_bytes()) as document:
        with patch.object(document, "_open_page", side_effect=RuntimeError("failure")):
            with pytest.raises(RuntimeError, match="failure"):
                document[0].get_snapshot()
        assert not document._page_snapshots
        with patch.object(document, "_open_page", wraps=document._open_page) as opened:
            snapshot = document[0].get_snapshot()
            assert document[0].get_snapshot() is snapshot
            assert opened.call_count == 1


def test_page_metadata_reuses_successful_reads():
    """Immutable documents are sized and rotated with only one page read, and failures are not written to the cache."""
    with PDFDocument(pdf_bytes()) as document:
        with patch.object(document, "_open_page", wraps=document._open_page) as opened:
            assert document.page_size(0) == (400, 500)
            assert document.page_size(0) == (400, 500)
            assert document.page_rotation(0) == 0
            assert opened.call_count == 1
        with patch.object(document, "_open_page", side_effect=RuntimeError("failed")):
            with pytest.raises(RuntimeError):
                document.page_size(1)
        assert 1 not in document._page_sizes


def test_raw_character_count_reuses_successful_read():
    """Repeated read raw count does not reconstruct textpage, zero value is also a cacheable success result."""
    from contextlib import nullcontext
    from unittest.mock import Mock

    with PDFDocument(pdf_bytes()) as document:
        text = Mock()
        text.count_chars.side_effect = [-1, 0]
        page = Mock()
        page.get_textpage.return_value = text
        with patch.object(document, "_open_page", return_value=nullcontext(page)) as opened:
            assert document.page_char_count(0) == -1
            assert document.page_char_count(0) == 0
            assert document.page_char_count(0) == 0
            assert opened.call_count == 2
            assert text.close.call_count == 2


def test_visible_snapshot_keeps_raw_count_for_limits():
    """The visibility filtered snapshot length does not replace the raw count, nor does the read count reopen the page."""
    from docvortex._compute_backend import get_native

    if get_native() is None:
        pytest.skip("Native snapshot unavailable")
    stream = BytesIO()
    canvas = Canvas(stream, pagesize=(200, 100))
    text = canvas.beginText(10, 50)
    text.setTextRenderMode(3)
    text.textLine("Hidden OCR still counts")
    canvas.drawText(text)
    canvas.save()
    data = stream.getvalue()
    with PDFDocument(data) as reference:
        expected = reference.page_char_count(0)
    with PDFDocument(data) as document:
        owner = document._get_owned_text_snapshot(0, visible_only=True)
        assert owner is not None
        assert owner.raw_char_count() == expected > owner.info()[0]
        with patch.object(document, "_open_page", side_effect=AssertionError("count must reuse snapshot metadata")):
            assert document.page_char_count(0) == expected
    assert owner.raw_char_count() == expected
