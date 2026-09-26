"""显式页面快照的生命周期、来源校验和共享证据回归。"""

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
    """生成包含文字、链接与路径的两页 PDF，避免依赖外部样本。"""
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
    """快照脱离原生资源，调用方修改导出字符不会影响后续读取。"""
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
    """字符、路径、链接、尺寸和旋转都来自快照，关闭后继续生成相同证据。"""
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
    """拒绝跨页、跨文档和显式几何混用，而不是静默接受错误证据。"""
    with PDFDocument(pdf_bytes()) as first, PDFDocument(pdf_bytes()) as second:
        snapshot = first[0].get_snapshot()
        for prepare in (prepare_text_evidence, prepare_table_page):
            for page in (first[1], second[0]):
                with pytest.raises(ValueError, match="different PDF page"):
                    prepare(page, snapshot=snapshot)
            with pytest.raises(ValueError, match="explicit geometry"):
                prepare(first[0], snapshot=snapshot, geometry=snapshot.text_geometry)


def test_snapshot_cache_does_not_retain_consumed_pages():
    """释放消费者后弱缓存立即允许页面证据回收，不形成文档级强引用。"""
    with PDFDocument(pdf_bytes()) as document:
        snapshot = document[0].get_snapshot()
        reference = weakref.ref(snapshot)
        del snapshot
        gc.collect()
        assert reference() is None
        assert not document._page_snapshots


def test_snapshot_extracts_once_and_failure_is_not_cached():
    """首次失败不留下半快照，成功提取后同页调用只打开一次页面。"""
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
    """不可变文档的尺寸和旋转只需一次页面读取，失败不会写入缓存。"""
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
