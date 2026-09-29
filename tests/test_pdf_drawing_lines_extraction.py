"""绘图线提取的按需构建与聚类合并等价性（#18 回归）。

`get_page_drawing_lines()` 不得构建随即丢弃的 `PDFPathInfo`；
`_merge_orientation_lines()` 以聚类首元素坐标代替整簇重扫，
两者输出都必须与全量构建、朴素重扫的实现逐位一致。
"""

from __future__ import annotations

import random
from importlib import import_module

import pytest

from docvortex.document.pdf import PDFDocument, native_objects
from docvortex.document.pdf.native_contracts import PDFDrawingLine

native_objects_module = import_module("docvortex.document.pdf.native_objects")


def _pdf(objects: list[bytes]) -> bytes:
    """按对象号顺序写出带 xref 表的最小合法 PDF（对象 1 是 catalog）。"""
    out, offsets = bytearray(b"%PDF-1.7\n"), []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def _vector_page_pdf(paths: int) -> bytes:
    """A4 页面上横竖交替的短描边线段，模拟 CAD 导出的路径密度。"""
    directions = ((40, 0), (0, 40))
    segments = [b"0.4 w"]
    for index in range(paths):
        column, row = index % 40, index // 40
        x, y = 20.0 + column * 14.0, 30.0 + row * 16.0
        dx, dy = directions[index % 2]
        segments.append(b"%.2f %.2f m %.2f %.2f l S" % (x, y, x + dx, y + dy))
    content = b"\n".join(segments)
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << >> /Contents 4 0 R >>",
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        ]
    )


def _reference_merge_orientation_lines(lines, page_size):
    """修复前的朴素实现：每条线对当前聚类整体重算最小轴坐标。"""
    tolerance = native_objects.DRAWING_LINE_MERGE_TOLERANCE
    if not lines:
        return []
    coordinate_clusters: list[list[PDFDrawingLine]] = []
    for line in sorted(
        lines, key=lambda item: (native_objects._line_axis_coordinate(item), native_objects._line_main_interval(item)[0])
    ):
        if (
            not coordinate_clusters
            or native_objects._line_axis_coordinate(line)
            - min(native_objects._line_axis_coordinate(item) for item in coordinate_clusters[-1])
            > tolerance
        ):
            coordinate_clusters.append([line])
        else:
            coordinate_clusters[-1].append(line)

    merged: list[PDFDrawingLine] = []
    for cluster in coordinate_clusters:
        interval_group: list[PDFDrawingLine] = []
        interval_end = float("-inf")
        for line in sorted(cluster, key=lambda item: native_objects._line_main_interval(item)[0]):
            line_start, line_end = native_objects._line_main_interval(line)
            if interval_group and line_start > interval_end + tolerance:
                combined = native_objects._combine_collinear_line_group(interval_group, page_size)
                if combined is not None:
                    merged.append(combined)
                interval_group = []
                interval_end = float("-inf")
            interval_group.append(line)
            interval_end = max(interval_end, line_end)
        if interval_group:
            combined = native_objects._combine_collinear_line_group(interval_group, page_size)
            if combined is not None:
                merged.append(combined)
    return merged


def _random_axis_lines(rng: random.Random, count: int, page_size) -> list[PDFDrawingLine]:
    """生成贴近与跨越合并容差的同轴/跨轴线段，覆盖聚类边界形态。"""
    lines = []
    for _ in range(count):
        horizontal = rng.random() < 0.5
        coordinate = rng.uniform(10.0, page_size[1] - 10.0) if horizontal else rng.uniform(10.0, page_size[0] - 10.0)
        coordinate += rng.choice((0.0, 0.0, 1.0, 1.9, 2.1, 4.0))
        start = rng.uniform(10.0, page_size[0] * 0.6 if horizontal else page_size[1] * 0.6)
        length = rng.choice((3.0, 20.0, 60.0, 200.0))
        if horizontal:
            candidate = native_objects._make_axis_drawing_line(
                (start, coordinate), (start + length, coordinate), 0.4, page_size
            )
        else:
            candidate = native_objects._make_axis_drawing_line(
                (coordinate, start), (coordinate, start + length), 0.4, page_size
            )
        if candidate is not None:
            lines.append(candidate)
    return lines


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_cluster_start_merge_matches_full_rescan(seed: int) -> None:
    """记聚类起点的合并结果与整簇重扫的朴素实现逐位一致。"""
    rng = random.Random(seed)
    page_size = (595.0, 842.0)
    lines = _random_axis_lines(rng, 260, page_size)

    assert native_objects._merge_orientation_lines(lines, page_size) == _reference_merge_orientation_lines(lines, page_size)


def test_drawing_lines_skip_path_info_construction(monkeypatch) -> None:
    """get_page_drawing_lines 不再构建随即丢弃的 PDFPathInfo，结果与全量提取一致。"""
    counter = {"calls": 0}
    original = native_objects._path_info_from_object

    def counting(*args, **kwargs):
        """统计绘图线专用入口意外构建路径信息的次数。"""
        counter["calls"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(native_objects, "_path_info_from_object", counting)

    data = _vector_page_pdf(600)
    with PDFDocument(data) as document:
        lines_only = document.get_page_drawing_lines(0)
    assert counter["calls"] == 0
    assert lines_only

    monkeypatch.setattr(native_objects, "_path_info_from_object", original)
    with PDFDocument(data) as document:
        with document._open_page(0) as page:
            from docvortex.document.pdf.native_coordinates import _normalize_pdf_page_bbox

            bbox = _normalize_pdf_page_bbox(page.get_bbox())
            full_lines, path_infos = native_objects._extract_page_paths_and_lines(page, bbox, 0)
            infos_only = native_objects._extract_page_paths_and_lines(page, bbox, 0, want_lines=False)[1]

    assert lines_only == full_lines
    assert path_infos == infos_only
    assert path_infos


def test_drawing_lines_match_vector_and_snapshot_entries() -> None:
    """独立线接口、矢量几何和页面快照的绘图线逐字段且按顺序一致。"""
    data = _vector_page_pdf(600)
    with PDFDocument(data) as document:
        direct = document.get_page_drawing_lines(0)
        vector = document.get_page_vector_geometry(0).drawing_lines
        snapshot = document.get_page_snapshot(0).vector_geometry.drawing_lines
        native_page = document._extract_native_page(0)

    assert direct == list(vector)
    assert direct == list(snapshot)
    assert direct == native_page.drawing_lines


def test_native_drawing_line_bridge_reports_execution_or_fallback() -> None:
    """后端报告必须区分新绘图线入口的实际执行与 Python 参考回退。"""
    from docvortex._compute_backend import backend_info, get_native

    native = get_native()
    with PDFDocument(_vector_page_pdf(60)) as document:
        document.get_page_drawing_lines(0)
    info = backend_info()
    if native is not None and hasattr(native, "read_pdfium_drawing_lines"):
        assert info["pdfium_drawing_line_bridge_calls"] >= 1
        assert info["pdfium_drawing_line_bridge_unavailable_reason"] is None
    else:
        assert info["pdfium_drawing_line_bridge_calls"] == 0
        assert info["pdfium_drawing_line_bridge_unavailable_reason"] is not None


def test_filled_path_and_transformed_form_use_reference_lines() -> None:
    """填充矩形及变换 Form 保留参考路径的几何和顺序。"""

    content = b"0 0 0 rg 10 10 40 2 re f q 2 0 0 2 0 0 cm /F1 Do Q"
    form_content = b"0.5 w 30 30 m 90 30 l S"
    data = _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /XObject << /F1 5 0 R >> >> /Contents 4 0 R >>",
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
            b"<< /Type /XObject /Subtype /Form /BBox [0 0 100 100] /Resources << >> /Length %d >>\nstream\n%s\nendstream"
            % (len(form_content), form_content),
        ]
    )
    with PDFDocument(data) as document:
        direct = document.get_page_drawing_lines(0)
        vector = document.get_page_vector_geometry(0).drawing_lines
        snapshot = document.get_page_snapshot(0).vector_geometry.drawing_lines
    assert direct
    assert direct == list(vector) == list(snapshot)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rust_lines_match_reference_for_page_rotation_and_offset(rotation: int, monkeypatch) -> None:
    """四种页面旋转和非零页面原点下，新入口与 Python 参考路径逐字段一致。"""

    content = b"0.4 w 20 30 m 100 30 l S 150 60 m 150 200 l S"
    data = _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [10 20 300 400] /Rotate %d /Resources << >> /Contents 4 0 R >>"
            % rotation,
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        ]
    )
    with PDFDocument(data) as document:
        accelerated = document.get_page_drawing_lines(0)

    from docvortex.document.pdf import _object_bridge

    def no_bridge(*_args):
        """强制第二次查询经过 Python 参考路径。"""
        return None

    monkeypatch.setattr(_object_bridge, "read_drawing_lines", no_bridge)
    with PDFDocument(data) as document:
        reference = document.get_page_drawing_lines(0)
    assert accelerated == reference
