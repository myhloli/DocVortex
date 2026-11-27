"""在独立阅读证据成立时展开 Form 包装，保留完整图示及整件原有样式上下文。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import re
import statistics
import unicodedata

from ....schema import BBox
from .form_roles import _form_masks, _has_independent_figures, _masked_line, form_owns_line, repeated_panel_bbox
from .geometry import _bbox_area, _bbox_axis_overlap_ratio, _bbox_overlap_in_first
from .layout_diagnostics import LayoutDiagnosticPage, _union_coverage
from .line_layout import _line_effective_height
from .layout_evidence import build_layout_evidence
from .models import _PageSource, _PreparedPage

_FIGURE_LABEL = re.compile(r"^\s*(?:fig(?:ure)?\.?\s*\d|图\s*\d)", re.I)


@dataclass(slots=True)
class FormExpansion:
    """携带完整候选及其自有的行内材料，防止换框后仍使用旧图块的成员。"""

    prepared: _PreparedPage
    evidence: LayoutDiagnosticPage | None
    style_lines: list
    link_lines: list
    boxes: tuple[BBox, ...]


def _external_figure_caption(source, form) -> bool:
    """外置图题保护单幅图；缩小页面示例可由外部较大字号图题共同保护。"""
    members = [line for line in source.lines if form_owns_line(line, form.text_indices)]
    if not members:
        return False
    inner_height = statistics.median(_line_effective_height(line, line.bbox) for line in members)
    for line in source.lines:
        if form_owns_line(line, form.text_indices) or not _FIGURE_LABEL.match(line.text):
            continue
        if _bbox_overlap_in_first(line.bbox, form.bbox) >= 0.5:
            continue
        if _bbox_axis_overlap_ratio(line.bbox, form.bbox, axis="x") < 0.5:
            continue
        height = _line_effective_height(line, line.bbox)
        gap = max(line.bbox[1] - form.bbox[3], form.bbox[1] - line.bbox[3], 0)
        if gap <= 4 * height or inner_height < 0.7 * height:
            return True
    return False


def _has_reading_lane(source, form, masks) -> bool:
    """同栏六条长行可分属不同自然段，段间留白不应把真实正文重新包装成图。"""
    lines = [
        line
        for line in source.lines
        if line.semantic_type is None
        and form_owns_line(line, form.text_indices)
        and not _FIGURE_LABEL.match(line.text)
        and not _masked_line(line.ink_bbox or line.bbox, masks)
    ]
    for angle in {line.angle for line in lines}:
        layout = build_layout_evidence([line for line in lines if line.angle == angle], source.page_size, angle=angle)
        for lane in layout.lanes:
            em = statistics.median(_line_effective_height(line, box) for line, box in lane.lines)
            long_rows = [
                box
                for line, box in lane.lines
                if box[2] - box[0] >= max(10 * em, 0.6 * (lane.right - lane.left))
                and min(abs(box[0] - lane.left), abs(box[2] - lane.right)) <= em
            ]
            centers = []
            for box in sorted(long_rows, key=lambda b: (b[1] + b[3]) / 2):
                center = (box[1] + box[3]) / 2
                if not centers or center - centers[-1] > 0.5 * em:
                    centers.append(center)
            if len(centers) >= 6:
                return True
    return False


def _has_exposed_text(source, form, masks) -> bool:
    """大多数可见文字在图形保护区外时确认文字包装，短段、封面与索引不必伪装成长正文。"""
    lines = [line for line in source.lines if line.semantic_type is None and form_owns_line(line, form.text_indices)]
    owned = {
        char["char_idx"]
        for line in lines
        for char in line.chars
        if char.get("char", "").strip() and isinstance(char.get("char_idx"), int)
    }
    exposed_lines = [line for line in lines if not _masked_line(line.ink_bbox or line.bbox, masks)]
    exposed = {
        char["char_idx"]
        for line in exposed_lines
        for char in line.chars
        if char.get("char", "").strip() and isinstance(char.get("char_idx"), int)
    }
    visible_paths = [
        path
        for path in source.path_infos
        if path.source_index in form.path_indices and (path.stroke_visible or path.segment_count > 5)
    ]
    # 图形占主体、只有稀疏标签时，不能把标签未落在图核内误解为文字页面。
    if visible_paths and _union_coverage(form.bbox, [line.ink_bbox or line.bbox for line in exposed_lines]) < 0.05 * _bbox_area(
        form.bbox
    ):
        return False
    rows = {(line.angle, line.visual_row_id if line.visual_row_id is not None else line.source_index) for line in exposed_lines}
    return bool(owned) and len(exposed) >= 0.8 * len(owned) and len(rows) >= 4


def _page_canvas(source, form) -> bool:
    """声明画布接近纸面或裁切纸面时支持页面包装，允许常见页边距而不依赖文件语义。"""
    if form.declared_bbox is None:
        return False
    width, height = source.page_size
    tolerance = (0.1 * width, 0.1 * height, 0.1 * width, 0.1 * height)
    return any(
        all(abs(a - b) <= limit for a, b, limit in zip(form.declared_bbox, frame, tolerance, strict=True))
        for frame in ((0.0, 0.0, width, height), form.media_bbox)
    )


def _text_container_or_page(source, form) -> bool:
    """局部绘图的字符标签不能单凭占比解除整图；纯文字容器和页面包装可使用该弱证据。"""
    if _page_canvas(source, form):
        return True
    return not form.image_bboxes and not any(
        path.source_index in form.path_indices and (path.fill_visible or path.stroke_visible) for path in source.path_infos
    )


def _is_ruled_glyph_sample(source: _PageSource, form) -> bool:
    """局部成对横线之间若多行重复展示同组独立字形，整幅保留其字体与空间对应关系。"""
    left, top, right, bottom = form.bbox
    tolerance = 0.05 * (right - left)
    rails = [
        rule.bbox
        for rule in source.drawing_lines
        if rule.orientation == "horizontal" and abs(rule.bbox[0] - left) <= tolerance and abs(rule.bbox[2] - right) <= tolerance
    ]
    if not (any(0 <= top - b[3] <= tolerance for b in rails) and any(0 <= b[1] - bottom <= tolerance for b in rails)):
        return False
    seen = {}
    for line in source.lines:
        if line.angle or not form_owns_line(line, form.text_indices):
            continue
        symbols = tuple(
            unicodedata.normalize("NFKC", t)
            for t in line.text.split()
            if len(t) == 1 and (t.isalpha() or unicodedata.category(t) == "Sm")
        )
        if len(set(symbols)) < 4:
            continue
        y = (line.bbox[1] + line.bbox[3]) / 2
        if symbols in seen and abs(y - seen[symbols]) > 0.6 * _line_effective_height(line, line.bbox):
            return True
        seen.setdefault(symbols, y)
    return False


def expandable_forms(source: _PageSource) -> tuple[BBox, ...]:
    """用非图内的连续阅读区域支持展开，不要求声明框碰到纸边或出现特定表格语义。"""
    chosen = []
    for form in source.form_infos:
        if form.bbox not in source.form_bboxes or not form.structure_valid or _bbox_area(form.bbox) <= 0:
            continue
        if (
            _external_figure_caption(source, form)
            or repeated_panel_bbox(source, form) is not None
            or _is_ruled_glyph_sample(source, form)
        ):
            continue
        # 覆盖整个容器的无描边填充矩形是背景证据，不能作为内部正文的图形遮罩。
        paths = [
            path
            for path in source.path_infos
            if not (
                path.source_index in form.path_indices
                and path.fill_visible
                and not path.stroke_visible
                and path.segment_count <= 5
            )
        ]
        local = replace(source, path_infos=paths, drawing_component_cache=[])
        graphic_masks = _form_masks(local, form)
        masks = list(graphic_masks)
        # 小填充框仍保护自己的节点文字，只阻止这些离散框被图核检测合并成过大的遮罩。
        masks.extend(
            path.bbox
            for path in source.path_infos
            if path.source_index in form.path_indices
            and path.fill_visible
            and not path.stroke_visible
            and path.segment_count <= 5
            and _bbox_overlap_in_first(form.bbox, path.bbox) < 0.8
        )
        if (
            _has_reading_lane(local, form, masks)
            or (_text_container_or_page(source, form) and _has_exposed_text(local, form, masks))
            or _has_independent_figures(local, form, graphic_masks)
        ):
            chosen.append(form.bbox)
    return tuple(chosen)


def prepare_form_expansion(source: _PageSource, kwargs: dict) -> FormExpansion | None:
    """仅给可展开页建立隔离候选，基线准备结果继续负责全篇样式学习。"""
    from .pipeline import _prepare_page_source

    if source.text_visibility_uncertain:
        return None
    boxes = expandable_forms(source)
    if not boxes:
        return None
    copied = deepcopy(source)
    style_lines = deepcopy(kwargs.get("style_lines"))
    link_lines = deepcopy(kwargs.get("link_lines"))
    evidence = LayoutDiagnosticPage(source.page_size) if kwargs.get("layout_page") is not None else None
    local_kwargs = dict(
        kwargs,
        style_lines=style_lines,
        link_lines=link_lines,
        _owned_script_inputs=None,
        layout_page=evidence,
        _expanded_form_bboxes=boxes,
    )
    prepared = _prepare_page_source(copied, **local_kwargs)
    return FormExpansion(prepared, evidence, style_lines, link_lines, boxes)
