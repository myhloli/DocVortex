"""Rust 自有 canonical 快照的真实读取、生命周期和完整语义差分。"""

from contextlib import closing
from dataclasses import fields, is_dataclass
from io import BytesIO
from pathlib import Path
import ctypes
import math
import pickle
from unittest.mock import patch

import pytest
import pypdfium2 as pdfium
from reportlab.pdfgen.canvas import Canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont

from docvortex._compute_backend import get_native
from docvortex.document.pdf import PDFDocument
from docvortex.document.pdf.pdfium import pdfium_guard
from docvortex.document.pdf.native_text_geometry import _extract_page_text_geometry
from docvortex.document.pdf.snapshot_bridge import read_text_snapshot, snapshot_bridge_info
from docvortex.document.pdf.text import _get_lines_from_chars_python
from docvortex.document.pdf.text._contracts import Bbox
from docvortex.analyzers.native.pdf.native_text import (
    _build_native_line_items_from_records,
    _build_native_line_items_from_chars,
)


@pytest.fixture
def native():
    """纯 Python 作业明确跳过，Rust 作业必须加载真正的快照扩展。"""
    value = get_native()
    if value is None:
        pytest.skip("Python backend")
    assert hasattr(value, "NativeTextSnapshot"), "rebuild extension with NativeTextSnapshot"
    return value


def _pdf(rotation=0, long=False):
    """生成旋转、多个字号、重复绘制、隐藏副本、裁剪以及空页。"""
    output = BytesIO()
    canvas = Canvas(output, pagesize=(450.5, 650.25))
    canvas.setPageRotation(rotation)
    for size, top in ((12, 550), (18, 510), (12, 470)):
        canvas.setFont("Helvetica", size)
        canvas.drawString(40, top, "Repeated text 123")
        canvas.drawString(40.2, top + 0.2, "Repeated text 123")
        text = canvas.beginText(40, top)
        text.setTextRenderMode(3)
        text.textOut("Repeated text 123")
        canvas.drawText(text)
    canvas.saveState()
    path = canvas.beginPath()
    path.rect(30, 410, 70, 25)
    canvas.clipPath(path, stroke=0, fill=0)
    canvas.drawString(20, 420, "clipped words and more")
    canvas.restoreState()
    if long:
        for row in range(26):
            canvas.drawString(30, 390 - row * 12, "boundary 1234567890 " * 3)
    canvas.showPage()
    canvas.showPage()
    canvas.save()
    return output.getvalue()


def _plain(value):
    """比较所有字段及数值，避免 dataclass 内部 Bbox 的对象地址干扰。"""
    if isinstance(value, Bbox):
        return (value.bbox, value.ensure_nonzero_area)
    if is_dataclass(value):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value) if field.compare}
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("visible", [False, True])
@pytest.mark.parametrize("extended", [False, True])
def test_snapshot_canonical_and_visual_parity(native, rotation, visible, extended):
    """独立 Python 参考覆盖 public/Flash 两策略与可选几何，所有成员字段必须相同。"""
    with pdfium_guard(), pdfium.PdfDocument(_pdf(rotation)) as document:
        for page_index in range(len(document)):
            with closing(document[page_index]) as page:
                legacy = _extract_page_text_geometry(page, include_extended_geometry=extended, visible_only=visible)
                with (
                    patch("docvortex._compute_backend.get_native", return_value=None),
                    patch("docvortex.document.pdf.text.extract.get_native", return_value=None),
                    patch("docvortex.document.pdf.text.dedup.get_native", return_value=None),
                ):
                    expected = _extract_page_text_geometry(page, include_extended_geometry=extended, visible_only=visible)
                snapshot = _extract_page_text_geometry(
                    page, include_extended_geometry=extended, visible_only=visible, compact=True
                )
                assert isinstance(snapshot, native.NativeTextSnapshot)
                assert snapshot.info()[2:] == (extended, visible)
            actual = snapshot.materialize_geometry()
            assert _plain(actual) == _plain(expected)
            assert _plain(actual) == _plain(legacy)
            assert pickle.dumps(actual) == pickle.dumps(legacy)
            for char in actual.chars:
                index = char["char_idx"]
                if index in actual.tight_bboxes:
                    assert actual.tight_bboxes[index] is char["tight_bbox"]
                if index in actual.origins:
                    assert actual.origins[index] is char["origin"]
            geometry, records = snapshot.prepare_visual_evidence(
                (650.25, 450.5) if rotation in (90, 270) else (450.5, 650.25), rotation, (0.0, 90.0, 270.0)
            )
            size = (650.25, 450.5) if rotation in (90, 270) else (450.5, 650.25)
            actual_lines = _build_native_line_items_from_records(records, size)
            expected_lines = _build_native_line_items_from_chars(expected.chars, size, page_rotation=rotation)
            assert _plain(actual_lines) == _plain(expected_lines)
            originals = {id(char) for char in geometry.chars}
            assert all(id(char) in originals for row in records for char in row[3])
            grouped_geometry, grouped = snapshot.prepare_grouped_evidence()
            assert _plain(grouped) == _plain(_get_lines_from_chars_python(expected.chars))
            originals = {id(char) for char in grouped_geometry.chars}
            assert all(id(char) in originals for line in grouped for span in line["spans"] for char in span["chars"])


def test_snapshot_is_native_before_any_python_char_materialization(native):
    """阻断旧字符物化器，证明首次快照及内部组行从未先生成 Python 字符字典。"""
    with PDFDocument(_pdf()) as document:
        with patch(
            "docvortex.document.pdf.native_text_geometry.get_chars", side_effect=AssertionError("Python chars materialized")
        ):
            snapshot = document[0].get_snapshot()
            private = document._extract_native_page(0)
        assert snapshot._geometry is None and isinstance(snapshot._native_text, native.NativeTextSnapshot)
        assert private._text_geometry is None and isinstance(private.native_text, native.NativeTextSnapshot)
        with patch("docvortex.document.pdf.text.get_lines_from_chars", side_effect=AssertionError("repacked chars")):
            assert snapshot.get_lines()
    geometry = snapshot.text_geometry
    geometry.chars[0]["font"]["name"] = "modified"
    geometry.chars[0]["bbox"].bbox[0] = -999
    assert snapshot.text_geometry.chars[0]["font"]["name"] != "modified"
    assert snapshot.text_geometry.chars[0]["bbox"].bbox[0] != -999


def test_snapshot_surrogates_cross_batch_and_invalid_code(native, monkeypatch):
    """代理项跨旧1024批界仍保留原索引，超出Unicode上限保持ValueError。"""
    from docvortex.document.pdf import snapshot_bridge

    factory = ctypes.WINFUNCTYPE if hasattr(ctypes, "WINFUNCTYPE") else ctypes.CFUNCTYPE
    original = snapshot_bridge.raw.FPDFText_GetUnicode

    def unicode_value(handle, index):
        """仅注入成对代理项，其余读取原始 PDFium。"""
        return {1023: 0xD83D, 1024: 0xDE00}.get(index, original(handle, index))

    monkeypatch.setattr(
        snapshot_bridge.raw, "FPDFText_GetUnicode", factory(original.restype, *original.argtypes)(unicode_value)
    )
    with pdfium_guard(), pdfium.PdfDocument(_pdf(long=True)) as document, closing(document[0]) as page:
        expected = _extract_page_text_geometry(page, include_extended_geometry=True)
        snapshot = _extract_page_text_geometry(page, include_extended_geometry=True, compact=True)
        assert _plain(snapshot.materialize_geometry()) == _plain(expected)
        assert any(
            char["char_idx"] == 1023 and char["source_indices"] == (1023, 1024)
            for char in snapshot.materialize_geometry().chars
        )

        def invalid_unicode(handle, index):
            """真实句柄的非法原始码值不得被替换成UFFFD。"""
            return 0x110000 if index == 0 else original(handle, index)

        monkeypatch.setattr(
            snapshot_bridge.raw, "FPDFText_GetUnicode", factory(original.restype, *original.argtypes)(invalid_unicode)
        )
        with pytest.raises(ValueError, match="chr"):
            _extract_page_text_geometry(page, include_extended_geometry=True, compact=True)


def test_snapshot_special_metadata_selects_reference_before_calculation(native):
    """自定义映射和非标准数值不进入新算法，原因可诊断且不伪报原生成功。"""
    from collections import UserDict

    with (
        pdfium_guard(),
        pdfium.PdfDocument(_pdf()) as document,
        closing(document[0]) as page,
        closing(page.get_textpage()) as textpage,
    ):
        before = snapshot_bridge_info()["native_text_snapshot_calls"]
        assert read_text_snapshot(textpage, [0, 0, 450, 650], 0, True) is None
        assert read_text_snapshot(textpage, list(page.get_bbox()), 0, True, UserDict()) is None
        assert snapshot_bridge_info()["native_text_snapshot_calls"] == before
        assert "nonstandard" in snapshot_bridge_info()["native_text_snapshot_unavailable_reason"]


def test_owned_geometry_risk_matches_line_reference(native):
    """页面级 geometry evidence 与逐行参考风险结论一致，身份副本强制回退。"""
    from copy import deepcopy
    from docvortex.analyzers.native.pdf import char_geometry, pipeline
    from docvortex.analyzers.native.pdf.native_text import _build_native_line_items_from_records

    with PDFDocument(_pdf(long=True)) as document:
        private = document._extract_native_page(0)
        geometry, records = private.native_text.prepare_visual_evidence(private.page_size, private.rotation, (0.0, 90.0, 270.0))
        lines = _build_native_line_items_from_records(records, private.page_size)
        owned = pipeline._prepare_owned_geometry_evidence(private.native_text, geometry.chars)
    args = ([lines], [geometry], [private.page_size])
    expected = char_geometry._document_requires_full_geometry(*args)
    actual = char_geometry._document_requires_full_geometry(*args, owned_geometry_inputs=[owned])
    assert actual == expected
    broken = deepcopy(lines)
    broken[0].chars = [dict(broken[0].chars[0])]
    fallback = char_geometry._document_requires_full_geometry(
        [broken], [geometry], [private.page_size], owned_geometry_inputs=[owned]
    )
    assert fallback == expected


@pytest.mark.parametrize(
    ("font_size", "line_height", "style_risk"),
    [(9.9, 14.925, True), (10.1, 15.075, False)],
)
def test_owned_geometry_uses_raw_font_size_for_style_risk(native, font_size, line_height, style_risk):
    """分组字号取整不能改变原始字号参与的跨页样式膨胀阈值。"""
    from docvortex.analyzers.native.pdf import char_geometry, pipeline

    output = BytesIO()
    canvas = Canvas(output, pagesize=(200, 200))
    for _page in range(2):
        canvas.setFont("Helvetica", font_size)
        for y in (150, 120, 90):
            canvas.drawString(20, y, "AAAA")
        canvas.showPage()
    canvas.save()

    lines_by_page, geometries, owned_inputs = [], [], []
    with PDFDocument(output.getvalue()) as document:
        for page_index in range(2):
            page = document._extract_native_page(page_index)
            geometry, records = page.native_text.prepare_visual_evidence(page.page_size, page.rotation, (0.0, 90.0, 270.0))
            lines = _build_native_line_items_from_records(records, page.page_size)
            for line in lines:
                line.effective_height = line_height
            lines_by_page.append(lines)
            geometries.append(geometry)
            owned_inputs.append(pipeline._prepare_owned_geometry_evidence(page.native_text, geometry.chars))
    args = (lines_by_page, geometries, [(200.0, 200.0)] * 2)
    expected = char_geometry._document_requires_full_geometry(*args)
    actual = char_geometry._document_requires_full_geometry(*args, owned_geometry_inputs=owned_inputs)
    assert expected.style is style_risk
    assert actual == expected


def test_owned_geometry_excludes_cjk_punctuation_from_anchors(native):
    """日文中点虽归入 cjk 文字组，仍不能作为几何风险锚点。"""
    from docvortex.analyzers.native.pdf import char_geometry, pipeline
    from docvortex.analyzers.native.pdf.models import _LineItem

    pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
    output = BytesIO()
    canvas = Canvas(output, pagesize=(200, 200))
    canvas.setFont("HeiseiKakuGo-W5", 12)
    for y in (140, 120):
        canvas.drawString(20, y, "・・・")
    canvas.save()
    with PDFDocument(output.getvalue()) as document:
        page = document._extract_native_page(0)
        geometry = page.native_text.materialize_geometry()
        members = [char for char in geometry.chars if char["char"] == "・"]
        owned = pipeline._prepare_owned_geometry_evidence(page.native_text, geometry.chars)
    assert len(members) == 6
    line = _LineItem("・・・・・・", (20.0, 45.0, 60.0, 90.0), 0, 0, chars=members, effective_height=12.0)
    args = ([[line]], [geometry], [(200.0, 200.0)])
    expected = char_geometry._document_requires_full_geometry(*args)
    actual = char_geometry._document_requires_full_geometry(*args, owned_geometry_inputs=[owned])
    assert expected.layout is False
    assert actual == expected


def test_owned_geometry_fallback_restarts_reference_run_namespace(native, monkeypatch):
    """启用 owned 通道后任一行失配必须整体重启参考路径，禁止两个从 0 起的 run 编号空间混用。"""
    from copy import deepcopy
    from docvortex.analyzers.native.pdf import char_geometry, pipeline

    with PDFDocument(_pdf(long=True)) as document:
        private = document._extract_native_page(0)
        geometry, records = private.native_text.prepare_visual_evidence(private.page_size, private.rotation, (0.0, 90.0, 270.0))
        lines = _build_native_line_items_from_records(records, private.page_size)
        owned = pipeline._prepare_owned_geometry_evidence(private.native_text, geometry.chars)
    expected = char_geometry._document_requires_full_geometry([lines], [geometry], [private.page_size])

    reference = char_geometry._document_requires_full_geometry_python
    calls = []

    def spy(*args, **kwargs):
        calls.append(args)
        return reference(*args, **kwargs)

    monkeypatch.setattr(char_geometry, "_document_requires_full_geometry_python", spy)
    broken = deepcopy(lines)
    broken[0].chars = [dict(broken[0].chars[0])]
    identity_miss = char_geometry._document_requires_full_geometry(
        [broken], [geometry], [private.page_size], owned_geometry_inputs=[owned]
    )
    page_missing = char_geometry._document_requires_full_geometry(
        [lines], [geometry], [private.page_size], owned_geometry_inputs=[None]
    )
    assert [call[0] for call in calls] == [[broken], [lines]]
    assert identity_miss == expected and page_missing == expected


def test_owned_table_and_geometry_evidence_match_reference(native, monkeypatch):
    """表格脚本与全文几何 owned 输入必须与逐行参考输出完全一致。"""
    from docvortex.analyzers.native.pdf import pipeline
    from docvortex.analyzers.native.pdf.table_text_styles import table_script_stats
    from docvortex._native import geometry_evidence_stats

    sample = Path(__file__).parents[1] / "demo/pdfs/demo3.pdf"
    before_table = table_script_stats()
    before_geometry = geometry_evidence_stats()
    with PDFDocument(str(sample)) as document:
        actual = pipeline._analyze_native_document(document)
    with PDFDocument(str(sample)) as document:
        monkeypatch.setattr(pipeline, "_prepare_owned_script_evidence", lambda *args, **kwargs: None)
        monkeypatch.setattr(pipeline, "_prepare_owned_geometry_evidence", lambda *args, **kwargs: None)
        expected = pipeline._analyze_native_document(document)
    assert actual == expected
    after_table = table_script_stats()
    after_geometry = geometry_evidence_stats()
    assert after_table[0] > before_table[0]
    assert after_table[1] > before_table[1]
    assert after_geometry[0] > before_geometry[0]
    assert after_geometry[1] > before_geometry[1]
    assert after_geometry[2] == before_geometry[2]


def test_python_textpage_wrapper_is_closed_after_native_snapshot(native, monkeypatch):
    """回退生命周期迁移后，页面只创建一次 Python textpage 并在快照后关闭。"""
    with pdfium_guard(), pdfium.PdfDocument(_pdf()) as document, closing(document[0]) as page:
        original = page.get_textpage
        textpages = []

        def tracked_textpage():
            """记录本次页面创建的 Python 包装，以核对退出后的句柄状态。"""
            result = original()
            textpages.append(result)
            return result

        monkeypatch.setattr(page, "get_textpage", tracked_textpage)
        before = snapshot_bridge_info()["native_text_snapshot_calls"]
        actual = _extract_page_text_geometry(page, include_extended_geometry=True, compact=True)
        assert isinstance(actual, native.NativeTextSnapshot)
        assert len(textpages) == 1 and textpages[0].raw is None
        with closing(original()) as textpage:
            expected = read_text_snapshot(textpage, list(page.get_bbox()), 0, True)
        assert actual.raw_char_count() == expected.raw_char_count()
        assert _plain(actual.materialize_geometry()) == _plain(expected.materialize_geometry())
        assert snapshot_bridge_info()["native_text_snapshot_calls"] == before + 2
        assert snapshot_bridge_info()["native_text_snapshot_unavailable_reason"] is None


def test_snapshot_keeps_public_geometry_dataclass_shape(native):
    """公开geometry仍只有原四字段；显式修改的geometry不能被快照旁路覆盖。"""
    from docvortex.analyzers.pdf import prepare_text_evidence

    with PDFDocument(_pdf()) as document:
        page = document[0]
        snapshot = page.get_snapshot()
        geometry = snapshot.text_geometry
        assert [field.name for field in fields(geometry)] == ["chars", "tight_bboxes", "origins", "loose_bboxes"]
        geometry.chars.clear()
        evidence = prepare_text_evidence(page, geometry=geometry, vector_geometry=snapshot.vector_geometry)
        assert evidence.geometry is geometry and evidence.geometry.chars == []


def test_owned_text_entry_has_no_vector_or_link_dependency(native):
    """方向等只读文本消费者不被提前路径提取约束，完整快照复用同一个Rust文本对象。"""
    with PDFDocument(_pdf()) as document:
        with (
            patch("docvortex.document.pdf._document._extract_page_paths_and_lines", side_effect=AssertionError("paths")),
            patch("docvortex.document.pdf._document._extract_page_link_annotations", side_effect=AssertionError("links")),
        ):
            owned = document._get_owned_text_snapshot(0)
            assert isinstance(owned, native.NativeTextSnapshot)
        with patch("docvortex.document.pdf.native_text_geometry.get_chars", side_effect=AssertionError("re-extracted")):
            snapshot = document[0].get_snapshot()
        assert snapshot._native_text is owned
        visible = document._get_owned_text_snapshot(0, visible_only=True)
        assert visible is not owned
        assert owned.info()[3] is False and visible.info()[3] is True


def test_snapshot_read_failure_does_not_retry_reference(native, monkeypatch):
    """已准入的PDFium失败直接传播，不通过旧路径读取第二次掩盖错误。"""
    from docvortex.document.pdf import snapshot_bridge

    factory = ctypes.WINFUNCTYPE if hasattr(ctypes, "WINFUNCTYPE") else ctypes.CFUNCTYPE
    original = snapshot_bridge.raw.FPDFText_GetLooseCharBox

    def failed_box(handle, index, rectangle):
        """保持ABI正确，仅注入PDFium失败返回值。"""
        return 0

    monkeypatch.setattr(
        snapshot_bridge.raw, "FPDFText_GetLooseCharBox", factory(original.restype, *original.argtypes)(failed_box)
    )
    with (
        PDFDocument(_pdf()) as document,
        patch("docvortex.document.pdf.native_text_geometry.get_chars", side_effect=AssertionError("reference retried")),
    ):
        with pytest.raises(pdfium.PdfiumError, match="Failed to get charbox"):
            document[0].get_snapshot()
        assert not document._page_snapshots and not document._owned_text_snapshots


def test_snapshot_long_font_decoding_and_signed_zero_font_sharing(native, monkeypatch):
    """长非法UTF8字体名与正负零字号遵守首值缓存，保留同次字体字典共享。"""
    from docvortex.document.pdf import snapshot_bridge

    factory = ctypes.WINFUNCTYPE if hasattr(ctypes, "WINFUNCTYPE") else ctypes.CFUNCTYPE
    font_reader = snapshot_bridge.raw.FPDFText_GetFontInfo
    size_reader = snapshot_bridge.raw.FPDFText_GetFontSize
    name = b"Snapshot-" + b"A" * 300 + b"\xff\0"

    def font_info(handle, index, buffer, capacity, flags):
        """按真实长度协议触发第二次缓冲读取，并保持非法字节替换语义。"""
        flags[0] = 32
        if capacity >= len(name):
            ctypes.memmove(buffer, name, len(name))
        return len(name)

    def font_size(handle, index):
        """对相邻字符交替返回零位型，缓存必须保留首次字号对象的符号。"""
        return -0.0 if index % 2 else 0.0

    monkeypatch.setattr(
        snapshot_bridge.raw, "FPDFText_GetFontInfo", factory(font_reader.restype, *font_reader.argtypes)(font_info)
    )
    monkeypatch.setattr(
        snapshot_bridge.raw, "FPDFText_GetFontSize", factory(size_reader.restype, *size_reader.argtypes)(font_size)
    )
    with pdfium_guard(), pdfium.PdfDocument(_pdf()) as document, closing(document[0]) as page:
        expected = _extract_page_text_geometry(page, include_extended_geometry=True)
        snapshot = _extract_page_text_geometry(page, include_extended_geometry=True, compact=True)
    actual = snapshot.materialize_geometry()
    assert _plain(actual) == _plain(expected)
    assert pickle.dumps(actual) == pickle.dumps(expected)
    fonts = {}
    for char in actual.chars:
        font = char["font"]
        key = tuple(font.items())
        assert fonts.setdefault(key, font) is font
        assert "\ufffd" in font["name"]
    assert math.copysign(1.0, actual.chars[0]["font"]["size"]) == 1.0


def test_public_text_snapshot_summaries_do_not_materialize_chars(native):
    """公开轻量入口生成精确粗行摘要，方向判断不创建字符、字体字典或Bbox。"""
    with PDFDocument(_pdf()) as document:
        page = document[0]
        with (
            patch("docvortex.document.pdf._document._extract_page_paths_and_lines", side_effect=AssertionError("paths")),
            patch("docvortex.document.pdf._document._extract_page_link_annotations", side_effect=AssertionError("links")),
        ):
            owned = page.get_text_snapshot()
        assert isinstance(owned, native.NativeTextSnapshot)
        _, lines = owned.prepare_grouped_evidence()
        expected = [
            {
                "bbox": tuple(line["bbox"].bbox),
                "rotation": line["rotation"],
                "spans": [{"text": span["text"]} for span in line["spans"]],
            }
            for line in lines
        ]
        with (
            patch(
                "docvortex.document.pdf.text._contracts.Bbox.__init__", side_effect=AssertionError("Char geometry materialized")
            ),
            patch(
                "docvortex.document.pdf.native_contracts.PDFPageTextGeometry",
                side_effect=AssertionError("Geometry materialized"),
            ),
        ):
            assert owned.get_line_summaries(0.7, 0.1) == expected
        assert page.get_snapshot()._native_text is owned


def test_public_text_snapshot_python_backend_does_not_extract(native):
    """参考后端能力探测立即返回None，不为了方向fallback多读一次字符。"""
    with PDFDocument(_pdf()) as document:
        page = document[0]
        with (
            patch("docvortex._compute_backend.get_native", return_value=None),
            patch.object(document, "_open_page", side_effect=AssertionError("unneeded extraction")),
        ):
            assert page.get_text_snapshot() is None
            assert document._get_owned_text_snapshot(0) is None


def test_private_snapshot_retains_constructor_keyword_and_lazy_cache(native):
    """旧假源可以继续传text_geometry，原生私有快照仅在访问时物化并保持该视图。"""
    from docvortex.document.pdf.native_contracts import _PDFPageSnapshot, PDFPageTextGeometry

    geometry = PDFPageTextGeometry([], {}, {})
    snapshot = _PDFPageSnapshot(
        page_size=(10.0, 10.0),
        rotation=0,
        text_geometry=geometry,
        drawing_lines=[],
        path_infos=[],
        image_infos=[],
        form_bboxes=[],
        signature_bboxes=[],
        link_annotations=[],
    )
    assert snapshot.text_geometry is geometry
    with PDFDocument(_pdf()) as document:
        snapshot = document._extract_native_page(0)
        assert snapshot._text_geometry is None
        view = snapshot.text_geometry
        assert snapshot.text_geometry is view


def test_owned_probe_does_not_materialize_reference_on_unsupported_metadata(native):
    """原生数值准入失败后只返回能力选择，不提前做一遍宿主随后还会重复的字符提取。"""
    with (
        PDFDocument(_pdf()) as document,
        patch("docvortex.document.pdf.snapshot_bridge.read_text_snapshot", return_value=None),
        patch(
            "docvortex.document.pdf.native_text_geometry.get_chars", side_effect=AssertionError("reference materialized twice")
        ),
    ):
        assert document[0].get_text_snapshot() is None
        assert not document._owned_text_snapshots


def test_owned_script_indices_match_plain_batches(native):
    """关闭文档后以重复、乱序及分段索引对照原列式上下标入口，边界非法必须报错。"""
    from docvortex.analyzers.native.pdf.inline import scripts
    from docvortex.analyzers.native.pdf._script_geometry import _coerce_finite_bbox
    import random

    with PDFDocument(_pdf()) as document:
        owner = document[0].get_text_snapshot()
    geometry = owner.materialize_geometry()
    prepared = owner.prepare_script_evidence(scripts._native_text_flags)
    assert prepared is not None
    before = native.script_snapshot_stats()
    randomizer = random.Random(572)
    for _ in range(24):
        indices = [randomizer.randrange(len(geometry.chars)) for _ in range(25)]
        chars = [geometry.chars[index] for index in indices]
        packed = scripts._pack_plain_script_input(chars, geometry.tight_bboxes, geometry.origins)
        offsets = [0, 6, 12, 25]
        expected = native.script_roles_plain_batch(*packed, offsets, _coerce_finite_bbox)
        assert prepared.classify_indices(indices, offsets) == expected
    assert native.script_snapshot_stats() == before + 24
    for indices, offsets in [([len(geometry.chars)], [0, 1]), ([0], [0, 0, 1]), ([0], [0, 2])]:
        with pytest.raises(ValueError, match="indices or offsets"):
            prepared.classify_indices(indices, offsets)


def test_owned_script_copied_members_use_reference(native):
    """行几何修复形成的字符副本不能凭相同 char_idx 复用旧快照；原成员仍按索引计算。"""
    from copy import deepcopy
    from docvortex.analyzers.native.pdf.inline import scripts
    from docvortex.analyzers.native.pdf.native_text import _build_native_line_items_from_records

    with PDFDocument(_pdf()) as document:
        owner = document[0].get_text_snapshot()
        size = document[0].size
    geometry, records = owner.prepare_visual_evidence(size, 0, [0.0])
    lines = _build_native_line_items_from_records(records, size)
    owned = scripts._prepare_owned_script_evidence(owner, geometry.chars)
    lines[0].chars = deepcopy(lines[0].chars)
    lines[0].chars[0]["bbox"].bbox[1] -= 3.0
    expected = scripts.detect_pdf_text_script_lines(
        lines, size, geometry.tight_bboxes, geometry.origins, all_chars=geometry.chars
    )
    before = native.script_snapshot_stats()
    actual = scripts.detect_pdf_text_script_lines(
        lines, size, geometry.tight_bboxes, geometry.origins, all_chars=geometry.chars, _owned_inputs=owned
    )
    assert pickle.dumps(actual) == pickle.dumps(expected)
    assert native.script_snapshot_stats() > before


def test_flash_consumes_and_releases_owned_script_pages(native, monkeypatch):
    """Flash 保留全文几何先行依赖；脚本复用与参考结果一致，并逐页释放原生输入队列。"""
    from docvortex.analyzers.native.pdf import pipeline

    before = native.script_snapshot_stats()
    with PDFDocument(_pdf()) as document:
        actual = pipeline._analyze_native_document(document)
    assert native.script_snapshot_stats() > before
    with monkeypatch.context() as patcher:
        patcher.setattr(pipeline, "_prepare_owned_script_evidence", lambda *args: None)
        with PDFDocument(_pdf()) as document:
            expected = pipeline._analyze_native_document(document)
    assert pickle.dumps(actual) == pickle.dumps(expected)
    with PDFDocument(_pdf()) as document:
        sources = pipeline._collect_document_sources(document)
    assert sources.page_owned_scripts and sources.page_owned_scripts[0] is not None
    pipeline._prepare_document_sources(sources)
    assert sources.page_owned_scripts == sources.page_sources == sources.page_text_geometries == []
