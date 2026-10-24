"""验证页内 Form 裁剪复用的几何、异常与句柄生命周期边界。"""

from contextlib import contextmanager
import ctypes
from io import BytesIO
from unittest.mock import Mock

import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen.canvas import Canvas

from docvortex.document.pdf import PDFDocument, _document, form_structure, native_objects


def _form_pdf(clip: str = "partial", rotation: int = 0, nested: bool = True) -> bytes:
    """构造重复调用、嵌套旋转斜切以及空/部分/无显式裁剪的真实 Form。"""
    stream = BytesIO()
    canvas = Canvas(stream, pagesize=(400, 500))
    canvas.beginForm("inner", 0, 0, 100, 100)
    if clip != "none":
        path = canvas.beginPath()
        path.rect(*(200, 200, 20, 20) if clip == "empty" else (10, 10, 50, 50))
        canvas.clipPath(path, stroke=0)
    canvas.rect(0, 0, 90, 90)
    canvas.drawString(15, 35, "Form label")
    canvas.endForm()
    if nested:
        canvas.beginForm("outer", -50, -50, 150, 150)
        canvas.translate(20, 10)
        canvas.rotate(15)
        canvas.skew(5, -10)
        canvas.doForm("inner")
        canvas.endForm()
    for x in (30, 230):
        canvas.saveState()
        canvas.translate(x, 200)
        canvas.doForm("outer" if nested else "inner")
        canvas.restoreState()
    canvas.drawString(30, 450, "Independent body text")
    canvas.save()
    writer = PdfWriter(clone_from=PdfReader(BytesIO(stream.getvalue())))
    writer.pages[0].rotate(rotation)
    writer.pages[0].cropbox.lower_left = (5, 7)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize("result", [None, (0.0, 0.0, 0.0, 0.0), (1.0, 2.0, 10.0, 20.0)])
def test_extent_reuses_none_and_empty_but_separates_geometry(monkeypatch, result):
    """无裁剪和空范围均只算一次；不同地址、页框或旋转不能共享。"""
    compute = Mock(return_value=result)
    monkeypatch.setattr(native_objects, "_clipped_form_extent", compute)
    cache = {}
    box = (0.0, 0.0, 100.0, 200.0)
    for _ in range(2):
        assert native_objects._shared_clipped_form_extent(ctypes.c_void_p(123), box, 0, cache) == result
    assert compute.call_count == 1
    for address, frame, angle in [(124, box, 0), (123, (1.0, 0.0, 100.0, 200.0), 0), (123, box, 90)]:
        assert native_objects._shared_clipped_form_extent(ctypes.c_void_p(address), frame, angle, cache) == result
    assert compute.call_count == 4


def test_direct_extent_calls_and_failures_are_not_cached(monkeypatch):
    """独立调用每次读取当前状态；计算异常不变成无裁剪或污染后续重试。"""
    compute = Mock(side_effect=[None, (1.0, 2.0, 3.0, 4.0), ValueError("broken"), None])
    monkeypatch.setattr(native_objects, "_clipped_form_extent", compute)
    args = (ctypes.c_void_p(123), (0.0, 0.0, 100.0, 200.0), 0)
    assert native_objects._shared_clipped_form_extent(*args, None) is None
    assert native_objects._shared_clipped_form_extent(*args, None) == (1.0, 2.0, 3.0, 4.0)
    cache = {}
    with pytest.raises(ValueError, match="broken"):
        native_objects._shared_clipped_form_extent(*args, cache)
    assert cache == {}
    assert native_objects._shared_clipped_form_extent(*args, cache) is None
    assert compute.call_count == 4


@pytest.mark.parametrize("clip", ["none", "partial", "empty"])
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("nested", [False, True])
def test_snapshot_matches_independent_extractors_and_computes_once(monkeypatch, clip, rotation, nested):
    """真实重复 Form 的快照与独立提取相同，矩阵和裁剪不改变根对象计算上界。"""
    compute = Mock(wraps=native_objects._clipped_form_extent)
    monkeypatch.setattr(native_objects, "_clipped_form_extent", compute)
    with PDFDocument(_form_pdf(clip, rotation, nested)) as document:
        with document._open_page(0) as page:
            box = page.get_bbox()
            expected_boxes = native_objects._extract_page_form_bboxes(page, box, rotation)
            expected_forms = (
                form_structure.extract_form_structure(page, box, rotation, document._get_form_resource_page(0))
                if expected_boxes
                else ()
            )
        assert compute.call_count == (4 if expected_boxes else 2)
        compute.reset_mock()
        snapshot = document._extract_native_page(0)
        assert compute.call_count == 2
    assert snapshot.form_bboxes == expected_boxes
    assert snapshot.form_infos == expected_forms
    if expected_boxes:
        roots = [form for form in snapshot.form_infos if form.parent_id is None]
        assert len(roots) == 2
        assert roots[0].matrix != roots[1].matrix
        assert roots[0].text_indices.isdisjoint(roots[1].text_indices)


@pytest.mark.parametrize("broken", [False, True])
def test_page_cache_cleared_before_close_and_never_reused(monkeypatch, broken):
    """成功及结构提取异常均在关页前清空；连续文档即使复用地址也重新计算。"""
    caches = []
    extract = _document._extract_page_form_bboxes
    open_page = PDFDocument._open_page
    compute = Mock(wraps=native_objects._clipped_form_extent)
    monkeypatch.setattr(native_objects, "_clipped_form_extent", compute)

    def capture(page, bbox, rotation, *, extent_cache=None):
        """保留缓存引用以检查真实页面退出时的数据释放。"""
        assert extent_cache == {}
        caches.append(extent_cache)
        return extract(page, bbox, rotation, extent_cache=extent_cache)

    @contextmanager
    def check_close(document, index):
        """在底层页面尚未关闭时验证异常清理，避免事后读取悬挂句柄。"""
        with open_page(document, index) as page:
            try:
                yield page
            finally:
                assert all(cache == {} for cache in caches)

    monkeypatch.setattr(_document, "_extract_page_form_bboxes", capture)
    monkeypatch.setattr(PDFDocument, "_open_page", check_close)
    if broken:
        monkeypatch.setattr(form_structure, "extract_form_structure", Mock(side_effect=ValueError("broken structure")))
    data = _form_pdf()
    for _ in range(3):
        with PDFDocument(data) as document:
            if broken:
                with pytest.raises(ValueError, match="broken structure"):
                    document._extract_native_page(0)
            else:
                assert document._extract_native_page(0).form_infos
    assert compute.call_count == 6
    assert len({id(cache) for cache in caches}) == 3
    assert all(cache == {} for cache in caches)


def test_damaged_root_stays_isolated_and_structure_retries(monkeypatch):
    """边界提取隔离单对象异常后，结构提取仍按原契约重新尝试该根对象。"""
    original = native_objects._clipped_form_extent
    calls = []

    def fail_first(raw, bbox, rotation):
        """只让首次根对象读取失败，后续调用使用真正 PDFium 数据。"""
        calls.append(ctypes.cast(raw, ctypes.c_void_p).value)
        if len(calls) == 1:
            raise ValueError("broken first extent")
        return original(raw, bbox, rotation)

    monkeypatch.setattr(native_objects, "_clipped_form_extent", fail_first)
    with PDFDocument(_form_pdf()) as document:
        snapshot = document._extract_native_page(0)
    assert len(snapshot.form_bboxes) == 1
    assert len([form for form in snapshot.form_infos if form.parent_id is None]) == 2
    assert len(calls) == 3
    assert calls[0] == calls[2] != calls[1]
