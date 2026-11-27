"""用印刷边界同时确定图形、侧置图例和原生字行的归属。"""

from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations, pairwise
from statistics import median
from typing import Any

from ....document.pdf.native_contracts import PDFPathInfo
from .geometry import (
    BBox,
    _bbox_area,
    _bbox_center_x,
    _bbox_intersects,
    _bbox_union_many,
    _clip_bbox,
    _expand_bbox,
)
from .graphics import _drawing_component_summaries, _image_members_to_content
from .layout_diagnostics import retain_line_members
from .models import _LineItem, _PageSource


def _inside(inner: BBox, outer: BBox) -> bool:
    """核验完整矩形落在容器里，允许绘制路径与边界齐平。"""
    return outer[0] <= inner[0] <= inner[2] <= outer[2] and outer[1] <= inner[1] <= inner[3] <= outer[3]


def _ink(line: _LineItem) -> BBox:
    """使用原字形框决定被裁片展示的文字，避免行框虚边拦截。"""
    return line.ink_bbox or line.bbox


def _framed_insets(paths: list[PDFPathInfo], page_size: tuple[float, float]):
    """从双矩形描边和贴在图边的成对填充、描线识别图内独立印刷面板。"""
    for frame in paths:
        if len(frame.rectangle_bboxes) != 2:
            continue
        outer, inner = sorted(frame.rectangle_bboxes, key=_bbox_area, reverse=True)
        thicknesses = (inner[0] - outer[0], inner[1] - outer[1], outer[2] - inner[2], outer[3] - inner[3])
        if (
            _bbox_area(outer) < 0.35 * page_size[0] * page_size[1]
            or min(thicknesses) <= 0
            or max(thicknesses) > 2 * min(thicknesses)
            or max(thicknesses) > 0.025 * min(outer[2] - outer[0], outer[3] - outer[1])
        ):
            continue
        # 白色底形及紧邻的同形描边共同限定图例，双矩形主边界裁去印刷出血。
        tolerance = max(thicknesses)
        for panel, outline in pairwise(paths):
            if (
                not panel.fill_visible
                or not panel.fill_rgba
                or min(panel.fill_rgba) < 245
                or not outline.stroke_visible
                or panel.segment_count != outline.segment_count
                or max(abs(a - b) for a, b in zip(panel.bbox, outline.bbox, strict=True)) > tolerance
            ):
                continue
            box = (
                max(panel.bbox[0], outer[0]),
                max(panel.bbox[1], outer[1]),
                min(panel.bbox[2], outer[2]),
                min(panel.bbox[3], outer[3]),
            )
            if (
                0.015 * _bbox_area(outer) <= _bbox_area(box) <= 0.35 * _bbox_area(outer)
                and (box[0] == outer[0] or box[2] == outer[2])
                and (box[1] == outer[1] or box[3] == outer[3])
            ):
                yield outer, box


def _paired_swatch_rows(paths: list[PDFPathInfo], labels: list[_LineItem], region: BBox) -> bool:
    """确认重复的矢量标记和标签有两条不同的对齐轨，排除只有分隔线的正文侧栏。"""
    if len(labels) < 4:
        return False
    em = median(line.em_height for line in labels)
    if em <= 0:
        return False
    marks = [
        p.bbox
        for p in paths
        if _inside(p.bbox, region) and 0.3 * em <= p.bbox[2] - p.bbox[0] <= 8 * em and 0 < p.bbox[3] - p.bbox[1] <= 2.5 * em
    ]
    pairs = Counter()
    for line in labels:
        left, top, _, bottom = _ink(line)
        for x, y, right, low in marks:
            if -0.1 * em <= left - right <= 2 * em and abs(y + low - top - bottom) <= em:
                # 同行双层笔画只记一次。相邻图标还须与标签在同一列轨上反复出现。
                step = 0.7 * em
                pairs[(round(x / step), round(left / step), round((top + bottom) / step))] = 1
    counts = Counter((x, left) for x, left, _ in pairs)
    return max(counts.values(), default=0) >= 4


def _distributed_ink(paths: list[PDFPathInfo], outer: BBox, inset: BBox) -> bool:
    """确认框内的非图例绘制密布于四个象限，排除页框与小图并存的普通页面。"""
    mid_x = (outer[0] + outer[2]) / 2
    mid_y = (outer[1] + outer[3]) / 2
    cells = Counter()
    for path in paths:
        box = path.bbox
        if not _inside(box, outer) or _inside(box, inset) or not path.stroke_visible:
            continue
        if box[2] - box[0] < 0.12 * (outer[2] - outer[0]) and box[3] - box[1] < 0.12 * (outer[3] - outer[1]):
            cells[(box[0] + box[2] >= 2 * mid_x, box[1] + box[3] >= 2 * mid_y)] += 1
    return len(cells) == 4 and min(cells.values()) >= 12


def _side_panels(source: _PageSource, core: BBox, em: float):
    """用栏沟以及相同栏宽的首尾印刷横线，给侧置图例的全部内容划定边界。"""
    for is_right in (False, True):
        edges = [
            line.bbox
            for line in source.drawing_lines
            if line.orientation == "horizontal"
            and line.bbox[2] - line.bbox[0] >= 7 * em
            and line.bbox[3] - line.bbox[1] <= 0.3 * em
            and core[1] - em <= line.bbox[1] <= core[3] + em
            and (0 < line.bbox[0] - core[2] <= 3 * em if is_right else 0 < core[0] - line.bbox[2] <= 3 * em)
        ]
        edges.sort(key=lambda edge: edge[1])
        for index, top in enumerate(edges):
            for bottom in edges[index + 1 :]:
                if max(abs(top[0] - bottom[0]), abs(top[2] - bottom[2])) > 0.25 * em:
                    continue
                # 遇到下一条同组分隔线就停止，不能跨越独立的提示或正文段。
                if bottom[1] - top[3] >= 5 * em:
                    candidate = (
                        min(top[0], bottom[0]) - 0.55 * em,
                        top[1],
                        max(top[2], bottom[2]) + 0.55 * em,
                        bottom[3],
                    )
                    if (box := _clip_bbox(candidate, source.page_size)) is not None:
                        yield box
                break


def _matching_index_tracks(first: list[_LineItem], second: list[_LineItem], axis: int, frame: BBox) -> bool:
    """要求两边标签镜像对齐、均匀分布并逐项相同，不根据字符类别判断。"""
    if len(first) < 4 or len(first) != len(second) or len({line.text.strip() for line in first}) < 4:
        return False

    def center(line: _LineItem) -> float:
        """取标签沿印刷边框方向的字形中心。"""
        box = _ink(line)
        return (box[axis] + box[axis + 2]) / 2

    first = sorted(first, key=center)
    second = sorted(second, key=center)
    centers = [center(line) for line in first]
    extent = centers[-1] - centers[0]
    stride = extent / (len(first) - 1)
    if extent < 0.6 * (frame[axis + 2] - frame[axis]):
        return False
    return all(abs(b - a - stride) <= 0.15 * stride for a, b in pairwise(centers)) and all(
        a.text.strip() == b.text.strip()
        and abs(center(a) - center(b)) <= 0.12 * stride
        and max(_ink(a)[axis + 2] - _ink(a)[axis], _ink(b)[axis + 2] - _ink(b)[axis]) <= 0.5 * stride
        for a, b in zip(first, second, strict=True)
    )


def _expand_printed_index_frame(source: _PageSource, frame: BBox) -> BBox:
    """只有同一填色外框和双轴镜像索引都成立时，才将边缘刻度并入原图。"""
    tolerance = 0.0001 * min(frame[2] - frame[0], frame[3] - frame[1])
    band_groups = defaultdict(dict)
    for path in source.path_infos:
        if not (path.fill_visible and path.fill_rgba and path.fill_rgba[3] == 255 and len(path.rectangle_bboxes) == 1):
            continue
        band = path.rectangle_bboxes[0]
        for axis, perpendicular in ((0, 1), (1, 0)):
            span = frame[axis + 2] - frame[axis]
            if (
                band[perpendicular] > frame[perpendicular] + tolerance
                or band[perpendicular + 2] < frame[perpendicular + 2] - tolerance
                or not tolerance < band[axis + 2] - band[axis] <= 0.07 * span
            ):
                continue
            for side in (0, 1):
                inner_edge = band[axis + 2 * (1 - side)]
                if abs(inner_edge - frame[axis + 2 * side]) <= tolerance:
                    band_groups[path.fill_rgba].setdefault((axis, side), set()).add(band)
    for group in band_groups.values():
        if len(group) != 4 or any(len(bands) != 1 for bands in group.values()):
            continue
        bands = {edge: next(iter(bounds)) for edge, bounds in group.items()}
        outer = _bbox_union_many([frame, *bands.values()])
        # 四条底板同时覆盖内框和外框之间四角，避开普通页眉页脚的分离色带。
        if any(
            not _inside(
                (xs[0], ys[0], xs[1], ys[1]),
                bands[0, xi],
            )
            and not _inside((xs[0], ys[0], xs[1], ys[1]), bands[1, yi])
            for xi, xs in enumerate(((outer[0], frame[0]), (frame[2], outer[2])))
            for yi, ys in enumerate(((outer[1], frame[1]), (frame[3], outer[3])))
        ):
            continue
        exterior = [line for line in source.lines if _bbox_intersects(_ink(line), outer) and not _inside(_ink(line), frame)]
        tracks = {edge: [line for line in exterior if _inside(_ink(line), band)] for edge, band in bands.items()}
        # 重叠角落不得用一次重复认领抵消另一源行的缺失。
        assigned = Counter(line.source_index for track in tracks.values() for line in track)
        if assigned == Counter(line.source_index for line in exterior) and all(
            _matching_index_tracks(tracks[axis, 0], tracks[axis, 1], 1 - axis, frame) for axis in (0, 1)
        ):
            return outer
    return frame


def _materialize_partition(
    source: _PageSource,
    boxes: tuple[BBox, ...],
    protected: list[BBox],
) -> tuple[list[dict[str, Any]], set[int]]:
    """两片同步认领全部完整源行；若穿过字形或争夺其他内容，则整组保留旧输出。"""
    if any(_bbox_intersects(box, bound) for box in boxes for bound in protected):
        return [], set()
    claims = set()
    blocks = []
    for box in boxes:
        members = [line for line in source.lines if _bbox_intersects(_ink(line), box)]
        if not members or any(line.source_index in claims or not _inside(_ink(line), box) for line in members):
            return [], set()
        claims.update(line.source_index for line in members)
        blocks.append(
            {
                "type": "image",
                "bbox": box,
                "angle": 0,
                "content": _image_members_to_content(members, source.page_size, spatial=True),
            }
        )
        retain_line_members(blocks[-1], members, enabled=source.collect_layout_diagnostics)
    return blocks, claims


def _outlined_region(source: _PageSource, core: BBox, tolerance: float) -> BBox | None:
    """用同一原生闭合路径和四边直线证实核心的物理边界，容许外侧投影。"""
    for path in source.path_infos:
        box = path.bbox
        if (
            path.form_depth
            or not path.stroke_visible
            or not 4 <= path.segment_count <= 6
            or _bbox_area(box) < 0.75 * _bbox_area(core)
            or not _inside(box, _expand_bbox(core, tolerance))
        ):
            continue
        if all(
            any(
                line.orientation == orientation
                and line.bbox[axis] <= edge + tolerance
                and line.bbox[axis + 2] >= edge - tolerance
                and abs(line.bbox[1 - axis] - box[1 - axis]) <= tolerance
                and abs(line.bbox[3 - axis] - box[3 - axis]) <= tolerance
                for line in source.drawing_lines
            )
            for axis, orientation in enumerate(("vertical", "horizontal"))
            for edge in (box[axis], box[axis + 2])
        ):
            return box
    return None


def _panel_with_heading(box: BBox, lines: list[_LineItem]) -> BBox:
    """仅合并边框正上方距离很近、按面板居中的单行局部题签。"""
    headings = [
        ink
        for line in lines
        if line.angle == 0
        and line.semantic_type is None
        and (ink := _ink(line))[0] >= box[0]
        and ink[2] <= box[2]
        and 0 <= box[1] - ink[3] <= 2 * (ink[3] - ink[1])
        and abs(_bbox_center_x(ink) - _bbox_center_x(box)) <= 0.1 * (box[2] - box[0])
    ]
    return _bbox_union_many([box, *headings]) if len(headings) == 1 else box


def _unframed_rail_panels(source: _PageSource, rail: BBox, framed: tuple[BBox, ...]):
    """用既有线组件和栏内纵向留白识别图件，保留只含正文的清空带。"""
    rail_lines = [
        line for line in source.lines if _inside(_ink(line), rail) and not any(_inside(_ink(line), bounds) for bounds in framed)
    ]
    if not rail_lines:
        return
    # 余留图件含已连通的绘图线组件；每个进入分区的对象必须有实际画线或原字墨迹。
    em = median(line.em_height for line in rail_lines)
    residual = [_ink(line) for line in rail_lines]
    components = [
        item.bbox
        for item in _drawing_component_summaries(source, max(2.0, 0.75 * em))
        if len(item.lines) >= 5
        and _inside(item.bbox, rail)
        and item.bbox[2] - item.bbox[0] >= 0.55 * (rail[2] - rail[0])
        and not any(_bbox_intersects(item.bbox, bounds) for bounds in framed)
    ]
    for component in components:
        box = component
        reach = 2 * em
        for text in sorted(residual, key=lambda ink: ink[3], reverse=True):
            if text[3] >= box[1] - reach and text[1] <= box[3] + reach:
                box = _bbox_union_many([box, text])
        # 整条宽正文或文字与已知图框相交时不从混合分区认领。
        width = component[2] - component[0]
        if all(ink[2] - ink[0] < 0.72 * width for ink in residual if _bbox_intersects(ink, box)) and not any(
            _bbox_intersects(box, bounds) for bounds in framed
        ):
            yield box


def _framed_graphic_companions(source: _PageSource, cores: list[BBox], protected: list[BBox]):
    """分配有独立物理边框的主体和同栏的两个附图，正文仍由正常文本管线处理。"""
    page_area = source.page_size[0] * source.page_size[1]
    tolerance = 0.002 * min(source.page_size)
    for core in cores:
        if _bbox_area(core) < 0.35 * page_area:
            continue
        body = _outlined_region(source, core, tolerance)
        if body is None:
            continue
        for left_side in (False, True):
            sides = [
                (candidate, _bbox_union_many([candidate, outline]))
                for candidate in cores
                if (candidate[2] < body[0] if left_side else candidate[0] > body[2])
                and body[1] <= candidate[1] <= candidate[3] <= body[3]
                and 0.01 * page_area <= _bbox_area(candidate) <= 0.2 * _bbox_area(body)
                and (outline := _outlined_region(source, candidate, tolerance)) is not None
            ]
            for (first, first_frame), (second, second_frame) in combinations(sides, 2):
                width = min(first[2] - first[0], second[2] - second[0])
                if max(abs(first[0] - second[0]), abs(first[2] - second[2])) > 0.15 * width:
                    continue
                if _bbox_intersects(first_frame, second_frame) or not any(
                    _paired_swatch_rows(source.path_infos, [line for line in source.lines if _inside(_ink(line), box)], box)
                    for box in (first_frame, second_frame)
                ):
                    continue
                panels = tuple(_panel_with_heading(box, source.lines) for box in (first_frame, second_frame))
                rail = (
                    max(0, min(first[0], second[0]) - 0.18 * width),
                    body[1],
                    min(source.page_size[0], max(first[2], second[2]) + 0.18 * width),
                    body[3],
                )
                boxes = (body, *panels)
                if any(_bbox_intersects(a, b) for a, b in combinations(boxes, 2)):
                    continue
                for panel in _unframed_rail_panels(source, rail, panels):
                    blocks, claims = _materialize_partition(source, (*boxes, panel), protected)
                    if blocks:
                        return blocks, claims
                blocks, claims = _materialize_partition(source, boxes, protected)
                if blocks:
                    return blocks, claims
    return [], set()


def build_native_graphic_partitions(
    source: _PageSource,
    graphic_cores: list[BBox],
    existing: list[BBox],
) -> tuple[list[dict[str, Any]], set[int]]:
    """按原始图框统一决定整图或非重叠的侧置图例，三种输出容器共享行所有权。"""
    if len(source.path_infos) < 96 or len(source.lines) < 16:
        return [], set()
    protected = [*source.trusted_text_bboxes, *existing]
    paths = source.path_infos
    for frame, inset in _framed_insets(paths, source.page_size):
        labels = [line for line in source.lines if line.angle == 0 and _inside(_ink(line), inset)]
        if _paired_swatch_rows(paths, labels, inset) and _distributed_ink(paths, frame, inset):
            expanded = _expand_printed_index_frame(source, frame)
            for box in dict.fromkeys((expanded, frame)):
                blocks, claims = _materialize_partition(source, (box,), protected)
                if blocks:
                    return blocks, claims

    for core in graphic_cores:
        if _bbox_area(core) < 0.2 * source.page_size[0] * source.page_size[1]:
            continue
        internal = [line for line in source.lines if _inside(_ink(line), core)]
        if len(internal) < 12:
            continue
        em = median(line.em_height for line in internal)
        for panel in _side_panels(source, core, em):
            labels = [line for line in source.lines if line.angle == 0 and _inside(_ink(line), panel)]
            if not _paired_swatch_rows(paths, labels, panel):
                continue
            padded = _expand_bbox(core, 1.2 * em)
            if panel[0] >= core[2]:
                padded = (padded[0], padded[1], min(padded[2], panel[0]), padded[3])
            else:
                padded = (max(padded[0], panel[2]), padded[1], padded[2], padded[3])
            # 小量边缘字由真实字形轮廓决定，而非把图侧文字按字号漫延带入。
            touches = [
                _ink(line) for line in source.lines if _bbox_intersects(_ink(line), core) and _inside(_ink(line), padded)
            ]
            body = _bbox_union_many([core, *touches])
            if _bbox_intersects(body, panel):
                continue
            blocks, claims = _materialize_partition(source, (body, panel), protected)
            if blocks:
                return blocks, claims
    return _framed_graphic_companions(source, graphic_cores, protected)
