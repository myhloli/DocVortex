"""用页内原始成员和版面几何判断 Flash 是否出现可复核的布局冲突。"""

from __future__ import annotations

import re
from array import array
from bisect import bisect_right
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from itertools import chain
from struct import iter_unpack
from typing import TYPE_CHECKING

from ....result import Diagnostic
from ....schema import BBox
from . import MISSING_VISUAL, READING_BOUNDARY, TEXT_OWNERSHIP, VISUAL_ROLE_CONFLICT
from .geometry import (
    _bbox_area,
    _bbox_axis_overlap_ratio,
    _bbox_center_y,
    _bbox_overlap_in_first,
    _bbox_union,
    _clip_bbox,
    _rotate_bbox_from_upright,
    _rotate_bbox_to_upright,
)

if TYPE_CHECKING:
    from ....document.pdf.native_contracts import PDFPathInfo
    from .layout_visual_role_risk import VisualTableEvidence
    from .models import _AxisLine, _LineItem

_GLYPHS = "_layout_glyph_ranges"
GlyphRanges = bytes
_BODY_TEXT = re.compile(r"[^\W\d_]+", re.UNICODE)


def _merge_ranges(ranges: Iterable[tuple[int, int]]) -> GlyphRanges:
    """把原始字符的半开区间合并，避免跨页保存字符对象或逐字索引。"""
    result: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if result and start <= result[-1][1]:
            previous, stop = result[-1]
            result[-1] = previous, max(stop, end)
        else:
            result.append((start, end))
    return array("I", chain.from_iterable(result)).tobytes()


def glyph_index_ranges(indices: Iterable[int]) -> GlyphRanges:
    """将页内 PDFium 字符身份压缩为区间。"""
    result = array("I")
    start = end = -1
    for index in sorted(indices):
        if not 0 <= index < 2**32 - 1:
            continue
        if index > end:
            if start >= 0:
                result.extend((start, end))
            start = index
        end = index + 1
    if start >= 0:
        result.extend((start, end))
    return result.tobytes()


def glyph_ranges(chars: Iterable[dict]) -> GlyphRanges:
    """读取已去重且可见的原始字形主索引，合成空白和控制字符不计入。"""
    return glyph_index_ranges(
        index
        for char in chars
        if isinstance(index := char.get("char_idx"), int)
        and not char.get("is_generated")
        and (value := char.get("char", "")).isprintable()
        and not value.isspace()
    )


def combined_line_ranges(lines: Iterable[_LineItem]) -> GlyphRanges:
    """合并已压缩的文本行，同时支持诊断开启时尚未释放的成员副本。"""
    return _merge_ranges(chain.from_iterable(_pairs(line.layout_glyph_ranges or glyph_ranges(line.chars)) for line in lines))


def retain_line_members(block: dict, lines: Iterable[_LineItem], *, enabled: bool) -> None:
    """给已经实际认领文字的块附加私有诊断证据。"""
    if enabled and (ranges := combined_line_ranges(lines)):
        existing = block.get(_GLYPHS)
        block[_GLYPHS] = _merge_ranges(chain(_pairs(existing), _pairs(ranges))) if existing else ranges


def retain_glyph_members(block: dict, ranges: GlyphRanges) -> None:
    """保存结构对象、单元格或投影器已确认承接的原始成员。"""
    if ranges:
        block[_GLYPHS] = ranges


def block_glyphs(block: dict) -> GlyphRanges:
    """汇总块和已装配子成员，父子内部的同一字形只保留一次。"""
    children = block.get("content")
    nested = (
        (block_glyphs(child) for child in children if isinstance(child, dict) and "bbox" in child)
        if isinstance(children, list)
        else ()
    )
    sources = [ranges for ranges in (block.get(_GLYPHS), combined_line_ranges(block.get("_text_lines", ())), *nested) if ranges]
    if len(sources) <= 1:
        return sources[0] if sources else b""
    return _merge_ranges(chain.from_iterable(_pairs(ranges) for ranges in sources))


def discard_block_glyphs(block: dict) -> None:
    """移除诊断内部字段，保证启用前后的公开模型输出相同。"""
    block.pop(_GLYPHS, None)
    if isinstance(block.get("content"), list):
        for child in block["content"]:
            if isinstance(child, dict) and "bbox" in child:
                discard_block_glyphs(child)


@dataclass(frozen=True, slots=True)
class _SourceRow:
    """原始正文候选行的位置、方向和可见字形区间。"""

    bbox: BBox
    angle: int
    glyphs: GlyphRanges


@dataclass(frozen=True, slots=True)
class _OutputBlock:
    """已通过输出归一化的块及其实际成员，全部几何统一为 PDF point。"""

    bbox: BBox
    kind: str
    glyphs: GlyphRanges
    angle: int = 0
    visual: bool = False


@dataclass(slots=True)
class LayoutDiagnosticPage:
    """单页诊断证据，直到最终块顺序确定后才执行判定。"""

    page_size: tuple[float, float]
    source_rows: list[_SourceRow] = field(default_factory=list)
    visual_sources: list[tuple[str, BBox]] = field(default_factory=list)
    marginal_visual_keys: dict[tuple[str, BBox], tuple] = field(default_factory=dict)
    columns: dict[int, tuple[tuple[float, float], ...]] = field(default_factory=dict)
    hard_breaks: list[tuple[GlyphRanges, GlyphRanges, BBox]] = field(default_factory=list)
    accepted_suppressed_glyphs: GlyphRanges = b""
    visual_tables: tuple[VisualTableEvidence, ...] = ()
    program_controls: tuple[BBox, ...] = ()

    def record_visual_tables(
        self,
        lines: list[_LineItem],
        drawing_lines: list[_AxisLine],
        path_infos: list[PDFPathInfo],
    ) -> None:
        """表图认领前验证原生表区，只跨阶段保存区域和行列计数。"""
        from .layout_visual_role_risk import collect_visual_tables

        self.visual_tables = collect_visual_tables(lines, drawing_lines, path_infos, self.page_size)

    def record_rows(self, lines: list[_LineItem], *, explicit_body: GlyphRanges = b"") -> None:
        """冻结正文源行与已有栏检测证据，避免单凭页面中线或字框交集认作多栏。"""
        from .layout_evidence import build_layout_evidence
        from .layout_program_risk import collect_program_controls

        self.program_controls = collect_program_controls(lines, self.page_size)
        body_lines = []
        for line in lines:
            box = _clip_bbox(line.ink_bbox or line.bbox, self.page_size)
            if box is None or line.semantic_type not in {None, "text"} or line.caption_start or line.formula_candidate_only:
                continue
            glyphs = glyph_ranges(line.chars)
            if not glyphs:
                continue
            words = _BODY_TEXT.findall(line.text)
            count = sum(end - start for start, end in _pairs(glyphs))
            confirmed = bool(explicit_body) and _ranges_overlap(glyphs, explicit_body)
            if not confirmed and (count < 8 or not (len(words) >= 2 or any(len(w) >= 6 and not w.isascii() for w in words))):
                continue
            body_lines.append(line)
            self.source_rows.append(_SourceRow(_rotate_bbox_to_upright(box, self.page_size, line.angle), line.angle, glyphs))
        for angle in {line.angle for line in body_lines}:
            lanes = build_layout_evidence(body_lines, self.page_size, angle=angle).lanes
            self.columns[angle] = tuple((lane.left, lane.right) for lane in lanes)

    def record_break(self, previous: _LineItem, current: _LineItem) -> None:
        """只保留组装器主动保护的同类正文硬段界及其两侧成员。"""
        if previous.semantic_type in {None, "text"} and current.semantic_type in {None, "text"}:
            a, b = previous.layout_glyph_ranges, current.layout_glyph_ranges
            if a and b:
                self.hard_breaks.append((a, b, _bbox_union(previous.bbox, current.bbox)))

    def record_visuals(self, kind: str, boxes: Iterable[BBox]) -> None:
        """仅保留达到整页面积 2% 的已过滤源图。"""
        min_area = 0.02 * self.page_size[0] * self.page_size[1]
        self.visual_sources.extend(
            (kind, box)
            for bounds in boxes
            if (box := _clip_bbox(bounds, self.page_size)) is not None and _bbox_area(box) >= min_area
        )

    def record_vector_visuals(self, boxes: Iterable[BBox]) -> None:
        """过滤与整页上沿或下沿齐平的通宽矢量装饰带，保留版心内的独立图组。"""
        width, height = self.page_size
        self.record_visuals(
            "矢量图组",
            (
                box
                for box in boxes
                if not (
                    box[0] <= 0.01 * width
                    and box[2] >= 0.99 * width
                    and (
                        box[1] <= 0.01 * height
                        and box[3] <= 0.10 * height
                        or box[1] >= 0.90 * height
                        and box[3] >= 0.99 * height
                    )
                )
            ),
        )

    def record_marginal_rasters(self, fingerprints: dict[BBox, str], protected: Iterable[BBox] = ()) -> None:
        """记录候选页边位图的原图指纹，已确认图题或引线的放置继续独立评分。"""
        protected_boxes = tuple(protected)
        for kind, box in self.visual_sources:
            if kind != "位图" or not (fingerprint := fingerprints.get(box)):
                continue
            if (position := _marginal_banner_position(box, self.page_size)) and not any(
                _bbox_overlap_in_first(box, graphic) >= 0.85 for graphic in protected_boxes
            ):
                self.marginal_visual_keys[kind, box] = (kind, position, fingerprint)

    def record_marginal_vectors(self, paths: Sequence[PDFPathInfo], protected: Iterable[BBox] = ()) -> None:
        """复用原生路径签名，跨页判定仅使用页边条的主体，忽略很薄的文字下划线。"""
        from .formulas import _vector_shape_signature

        protected_boxes = tuple(protected)
        for kind, box in self.visual_sources:
            if kind != "矢量图组" or not (position := _marginal_banner_position(box, self.page_size)):
                continue
            if any(_bbox_overlap_in_first(box, graphic) >= 0.85 for graphic in protected_boxes):
                continue
            area = _bbox_area(box)
            min_height = 0.05 * (box[3] - box[1])
            members = [
                path
                for path in paths
                if path.bbox[3] - path.bbox[1] >= min_height
                and _bbox_area(path.bbox) >= 0.002 * area
                and _bbox_overlap_in_first(path.bbox, box) >= 0.95
            ]
            # 同位置的单一矩形不足以证明图案重复，必须另外有原生绘图成员。
            if len(members) >= 2:
                structure = _vector_shape_signature(members, box)
                ink = sorted((path.segment_count, path.fill_rgba or (), path.stroke_visible) for path in members)
                identity = sha256(repr((structure, ink)).encode()).hexdigest()
                self.marginal_visual_keys[kind, box] = (kind, position, identity)

    def diagnose(self, blocks: list[dict], page_index: int) -> list[Diagnostic]:
        """从当前页最终模型列表生成稳定原因码，各原因至多一条。"""
        width, height = self.page_size
        output = [
            _OutputBlock(
                (box[0] * width, box[1] * height, box[2] * width, box[3] * height),
                block["type"],
                block_glyphs(block),
                int(block.get("angle", 0) or 0),
                block["type"] in {"image", "chart", "equation", "image_block"}
                or (block["type"] == "table" and bool(block.get("image_base64"))),
            )
            for block in blocks
            if (box := block.get("bbox"))
        ]
        result: list[Diagnostic] = []
        for code, message in (
            (TEXT_OWNERSHIP, self._text_conflict(output)),
            (MISSING_VISUAL, self._visual_conflict(output)),
            (READING_BOUNDARY, self._reading_conflict(output)),
            (VISUAL_ROLE_CONFLICT, self._visual_table_conflict(output)),
        ):
            if message is not None:
                result.append(Diagnostic(code, message, page_index))
        return result

    def _text_conflict(self, output: list[_OutputBlock]) -> str | None:
        """按正文自然相邻行计算无主和跨独立块重复的真实字形。"""
        if not self.source_rows:
            return None
        owners: list[tuple[int, BBox]] = []
        handled = duplicate = 0
        for block in output:
            members = _glyph_mask(block.glyphs)
            if not members:
                continue
            if handled & members:
                for seen, bounds in owners:
                    shared = members & seen
                    if (
                        shared
                        and _bbox_overlap_in_first(bounds, block.bbox) < 0.99
                        and _bbox_overlap_in_first(block.bbox, bounds) < 0.99
                    ):
                        duplicate |= shared
            handled |= members
            owners.append((members, block.bbox))
        abnormal = ~(handled | _glyph_mask(self.accepted_suppressed_glyphs)) | duplicate
        lanes: dict[tuple[int, int | None], list[_SourceRow]] = {}
        for row in self.source_rows:
            lane = _column_for(row.bbox, self.columns.get(row.angle, ()))
            lanes.setdefault((row.angle, lane), []).append(row)
        for key, rows in lanes.items():
            previous = None
            for row in sorted(rows, key=lambda line: (line.bbox[1], line.bbox[0])):
                members = _glyph_mask(row.glyphs)
                n, bad = members.bit_count(), (members & abnormal).bit_count()
                if not output and bad:
                    bounds = _rotate_bbox_from_upright(row.bbox, self.page_size, row.angle)
                    return f"可见正文源行 {bad}/{n} 个字形无归属，页面未输出内容，区域 {_region(bounds)}"
                if bad * 2 <= n:
                    previous = None
                    continue
                if previous is not None and (key[1] is not None or len(self.columns.get(key[0], ())) < 2):
                    before, count, total = previous
                    if _adjacent_rows(before.bbox, row.bbox):
                        bounds = _rotate_bbox_from_upright(_bbox_union(before.bbox, row.bbox), self.page_size, row.angle)
                        return f"两条连续正文源行的异常字形为 {count}/{total}、{bad}/{n}，区域 {_region(bounds)}"
                previous = row, bad, n
        return None

    def _visual_conflict(self, output: list[_OutputBlock]) -> str | None:
        """用实际计划裁出的图面框并集衡量可见位图和高置信矢量图核心。"""
        covers = [block.bbox for block in output if block.visual]
        page_area = self.page_size[0] * self.page_size[1]
        for kind, bounds in self.visual_sources:
            area = _bbox_area(bounds)
            covered = _union_coverage(bounds, covers) / area
            if covered < 0.5:
                return f"{kind}框占整页 {area / page_area:.1%}，图面覆盖 {covered:.1%}，区域 {_region(bounds)}"
        return None

    def _visual_table_conflict(self, output: list[_OutputBlock]) -> str | None:
        """核查源表区由图片覆盖且缺少对应表格容器的角色冲突。"""
        if not self.visual_tables:
            return None
        images = [block.bbox for block in output if block.kind in {"image", "chart", "image_block"}]
        if not images:
            return None
        tables = [block.bbox for block in output if block.kind == "table"]
        for region in self.visual_tables:
            area = _bbox_area(region.bbox)
            if _union_coverage(region.bbox, images) < 0.9 * area or _union_coverage(region.bbox, tables) >= 0.75 * area:
                continue
            return (
                f"原生表区的{region.support}支持 {region.rows} 行及至少 {region.columns} 列，"
                f"图件覆盖表区且缺少对应表格容器，区域 {_region(region.bbox)}"
            )
        return None

    def _reading_conflict(self, output: list[_OutputBlock]) -> str | None:
        """报告明确硬段界或同一双栏正文带中的跨栏折返。"""
        body = [block for block in output if block.kind == "text"]
        for before, after, bbox in self.hard_breaks:
            if any(_ranges_overlap(before, block.glyphs) and _ranges_overlap(after, block.glyphs) for block in body):
                return f"最终正文块跨过两条源行之间的硬段界，区域 {_region(bbox)}"

        for angle, columns in self.columns.items():
            if len(columns) != 2 or columns[0][1] >= columns[1][0]:
                continue
            # 通栏正文/标题与横跨两栏的图表在物理 y 轴切带，防止输出早晚改变分带。
            local = [(block, _rotate_bbox_to_upright(block.bbox, self.page_size, angle)) for block in output]
            cuts = sorted(
                _bbox_center_y(box)
                for block, box in local
                if block.kind in {"doc_title", "paragraph_title", "image", "chart", "table", "equation", "text"}
                and _crosses_columns(box, columns)
            )
            band_support = self._body_column_support(angle, columns, cuts, local)

            bands: dict[int, list[tuple[int, BBox]]] = {}
            for block, box in local:
                if block.kind != "text" or block.angle != angle:
                    continue
                lane = _column_for(box, columns)
                if lane is not None:
                    bands.setdefault(bisect_right(cuts, _bbox_center_y(box)), []).append((lane, box))
            for band, sequence in bands.items():
                if band not in band_support:
                    continue
                switches = [item for i, item in enumerate(sequence) if i == 0 or item[0] != sequence[i - 1][0]]
                for first, second, third in zip(switches, switches[1:], switches[2:]):
                    # 左右栏需在当前带有纵向重叠，独立上下双栏布局不视为折返。
                    side = _bbox_union(first[1], third[1])
                    if _bbox_axis_overlap_ratio(side, second[1], axis="y") > 0:
                        names = "左→右→左" if first[0] == 0 else "右→左→右"
                        bounds = _rotate_bbox_from_upright(_bbox_union(side, second[1]), self.page_size, angle)
                        return f"同一双栏正文带的输出顺序为 {names}，区域 {_region(bounds)}"
        return None

    def _body_column_support(
        self,
        angle: int,
        columns: tuple[tuple[float, float], ...],
        cuts: list[float],
        blocks: list[tuple[_OutputBlock, BBox]],
    ) -> set[int]:
        """只让同一带里连续印刷的正文建立两栏，图内文字、表格与标题不能作为栏证据。"""
        obstacles = [
            box
            for block, box in blocks
            if block.visual or block.kind in {"table", "doc_title", "paragraph_title", "header", "footer", "caption"}
        ]
        support: dict[int, tuple[set[float], set[float]]] = {}
        for row in self.source_rows:
            if row.angle != angle or any(_bbox_overlap_in_first(row.bbox, box) >= 0.6 for box in obstacles):
                continue
            lane = _column_for(row.bbox, columns)
            if lane is not None:
                baseline = _bbox_center_y(row.bbox)
                support.setdefault(bisect_right(cuts, baseline), (set(), set()))[lane].add(round(baseline, 1))
        return {
            band
            for band, (left, right) in support.items()
            if len(left) >= 3 and len(right) >= 3 and max(min(left), min(right)) <= min(max(left), max(right))
        }


def _pairs(ranges: GlyphRanges) -> Iterable[tuple[int, int]]:
    """惰性解包固定宽度区间，跨页阶段仅持有连续字节。"""
    return iter_unpack("=II", ranges)


def suppress_repeated_marginal_visuals(pages: Iterable[LayoutDiagnosticPage]) -> None:
    """仅移除至少三页同位置重复的页边装饰证据，页面输出和正文图源不受影响。"""
    pages = tuple(pages)
    counts = Counter(key for page in pages for key in set(page.marginal_visual_keys.values()))
    for page in pages:
        page.visual_sources[:] = [source for source in page.visual_sources if counts[page.marginal_visual_keys.get(source)] < 3]


def _marginal_banner_position(box: BBox, page_size: tuple[float, float]) -> tuple[float, ...] | None:
    """只接纳位于上沿或下沿的扁宽栏眉，正文图和较大水印仍由既有规则处理。"""
    width, height = page_size
    if (
        width <= 0
        or height <= 0
        or box[2] - box[0] < 0.45 * width
        or box[3] - box[1] > 0.11 * height
        or not (box[3] <= 0.135 * height or box[1] >= 0.865 * height)
    ):
        return None
    return tuple(round(value / page_size[index % 2], 2) for index, value in enumerate(box))


def _glyph_mask(ranges: GlyphRanges) -> int:
    """用单页位集合合并、相交并计数原始字形，无需逐字分配 Python 对象。"""
    result = 0
    for start, end in _pairs(ranges):
        result |= ((1 << (end - start)) - 1) << start
    return result


def _ranges_overlap(first: GlyphRanges, second: GlyphRanges) -> bool:
    """线性检查两组有序区间是否拥有同一原始字形。"""
    left, right = iter(_pairs(first)), iter(_pairs(second))
    a, b = next(left, None), next(right, None)
    while a is not None and b is not None:
        if a[1] <= b[0]:
            a = next(left, None)
        elif b[1] <= a[0]:
            b = next(right, None)
        else:
            return True
    return False


def _column_for(box: BBox, columns: tuple[tuple[float, float], ...]) -> int | None:
    """只接受主体与一个栏带重合的行块，跨栏块由屏障处理。"""
    width = box[2] - box[0]
    matches = [n for n, (a, b) in enumerate(columns) if min(box[2], b) - max(box[0], a) >= 0.6 * width]
    return matches[0] if len(matches) == 1 else None


def _crosses_columns(box: BBox, columns: tuple[tuple[float, float], ...]) -> bool:
    """确认通栏区域实际穿过栏沟并深入两侧正文栏。"""
    return all(min(box[2], b) - max(box[0], a) >= 0.25 * (b - a) for a, b in columns)


def _adjacent_rows(first: BBox, second: BBox) -> bool:
    """同栏行需有水平重合和正常相邻行距，避免跨越大段空白或连计同基线拆分。"""
    h = max(first[3] - first[1], second[3] - second[1])
    dy = _bbox_center_y(second) - _bbox_center_y(first)
    return 0.35 * h <= dy <= 2.5 * h and _bbox_axis_overlap_ratio(first, second, axis="x") >= 0.4


def _region(bbox: BBox) -> str:
    """格式化便于对照原页的 PDF point 矩形。"""
    return f"({', '.join(f'{point:.1f}' for point in bbox)}) pt"


def _union_coverage(source: BBox, covers: list[BBox]) -> float:
    """裁到源图后按 x 切片累加 y 轴区间并集，同一区域不会重复计数。"""
    intersections = [
        (max(source[0], box[0]), max(source[1], box[1]), min(source[2], box[2]), min(source[3], box[3]))
        for box in covers
        if min(source[2], box[2]) > max(source[0], box[0]) and min(source[3], box[3]) > max(source[1], box[1])
    ]
    area = 0.0
    x_values = sorted({value for box in intersections for value in (box[0], box[2])})
    for left, right in zip(x_values, x_values[1:]):
        spans = sorted((box[1], box[3]) for box in intersections if box[0] <= left < box[2])
        end = source[1]
        for start, stop in spans:
            area += (right - left) * max(0.0, stop - max(end, start))
            end = max(end, stop)
    return area
