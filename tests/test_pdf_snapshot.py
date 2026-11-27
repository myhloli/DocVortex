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


@pytest.mark.parametrize("first_visible", [False, True])
def test_snapshot_caches_visibility_modes_separately(first_visible: bool) -> None:
    """同页按可见性分别复用快照，切换读取次序也不会把被覆盖的文字泄漏给另一种模式。"""
    stream = BytesIO()
    canvas = Canvas(stream, pagesize=(200, 100))
    canvas.drawString(10, 65, "Covered")
    canvas.setFillColorRGB(1, 1, 1)
    canvas.rect(5, 55, 100, 30, fill=1, stroke=0)
    canvas.setFillColorRGB(0, 0, 0)
    canvas.drawString(10, 25, "Visible")
    canvas.linkURL("https://example.com", (10, 20, 50, 40))
    canvas.save()

    with PDFDocument(stream.getvalue()) as document:
        page = document[0]
        first = page.get_snapshot(visible_only=first_visible)
        second = page.get_snapshot(visible_only=not first_visible)
        visible, ordinary = (first, second) if first_visible else (second, first)
        assert page.get_snapshot() is ordinary
        assert document.get_page_snapshot(0, visible_only=True) is visible
    assert "".join(c["char"] for c in ordinary.text_geometry.chars if c["char"].strip()) == "CoveredVisible"
    assert "".join(c["char"] for c in visible.text_geometry.chars if c["char"].strip()) == "Visible"
    assert visible.vector_geometry == ordinary.vector_geometry
    assert visible.link_annotations == ordinary.link_annotations


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


def test_raw_character_count_reuses_successful_read():
    """重复读取原始计数不重建 textpage，零值也属于可缓存的成功结果。"""
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
    """可见性过滤后的快照长度不能替代原始计数，读取计数也不重新打开页面。"""
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
