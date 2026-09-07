"""PDF 填充单元格候选；保留原有认领顺序与判定规则。"""

from __future__ import annotations
from ....schema import BBox
from ....document.pdf.document import PDFPathInfo
from .models import _TableCandidate
from .geometry import (
    _bbox_area,
    _bbox_overlap_in_smaller,
)

from .table_constants import (
    _FILLED_GRID_MAX_PAGE_AREA_RATIO,
    _FILLED_GRID_MIN_PAGE_AREA_RATIO,
    _FILLED_GRID_MIN_PAGE_HEIGHT_RATIO,
    _FILLED_GRID_MIN_PAGE_WIDTH_RATIO,
)


def _detect_filled_grid_table_candidates(
    path_infos: list[PDFPathInfo],
    page_size: tuple[float, float],
    excluded_bboxes: list[BBox] | None = None,
) -> list[_TableCandidate]:
    """仅按根层填充矩形的嵌套行带识别精确表格外框。"""

    page_width, page_height = page_size
    page_area = page_width * page_height
    if page_area <= 0:
        return []
    excluded_bboxes = excluded_bboxes or []
    rectangles = [
        path_info.bbox
        for path_info in path_infos
        if path_info.form_depth == 0
        and path_info.fill_visible
        and path_info.segment_count == 5
        and _bbox_area(path_info.bbox) > 0
    ]
    evidence: list[tuple[_TableCandidate, int, float, float]] = []
    for outer_bbox in rectangles:
        outer_width = outer_bbox[2] - outer_bbox[0]
        outer_height = outer_bbox[3] - outer_bbox[1]
        outer_area = _bbox_area(outer_bbox)
        if not (
            _FILLED_GRID_MIN_PAGE_AREA_RATIO <= outer_area / page_area <= _FILLED_GRID_MAX_PAGE_AREA_RATIO
            and outer_width >= _FILLED_GRID_MIN_PAGE_WIDTH_RATIO * page_width
            and outer_height >= _FILLED_GRID_MIN_PAGE_HEIGHT_RATIO * page_height
        ):
            continue
        if any(_bbox_overlap_in_smaller(outer_bbox, excluded_bbox) >= 0.5 for excluded_bbox in excluded_bboxes):
            continue

        cells = _select_maximal_filled_grid_cells(rectangles, outer_bbox)
        bands = _group_filled_grid_cells_into_bands(cells, outer_bbox)
        accepted_bands = [band for band in bands if _filled_grid_band_covers_outer_width(band, outer_bbox)]
        if len(accepted_bands) < 4:
            continue
        accepted_bands.sort(
            key=lambda band: (
                min(cell[1] for cell in band),
                min(cell[0] for cell in band),
            )
        )
        covered_height = sum(max(cell[3] for cell in band) - min(cell[1] for cell in band) for band in accepted_bands)
        vertical_coverage = covered_height / outer_height
        if vertical_coverage < 0.75:
            continue
        if any(
            min(cell[1] for cell in next_band) - max(cell[3] for cell in current_band) > 0.05 * outer_height
            for current_band, next_band in zip(
                accepted_bands,
                accepted_bands[1:],
            )
        ):
            continue
        candidate = _TableCandidate(
            bbox=outer_bbox,
            local_bbox=outer_bbox,
            angle=0,
            score=float(100.0 + len(accepted_bands) + vertical_coverage),
            core_bbox=outer_bbox,
            line_indices=set(),
        )
        evidence.append(
            (
                candidate,
                len(accepted_bands),
                vertical_coverage,
                outer_area,
            )
        )

    output: list[_TableCandidate] = []
    for candidate, _band_count, _coverage, _outer_area in sorted(
        evidence,
        key=lambda item: (item[1], item[2], item[3]),
        reverse=True,
    ):
        if any(_bbox_overlap_in_smaller(candidate.bbox, accepted.bbox) >= 0.9 for accepted in output):
            continue
        output.append(candidate)
    return sorted(output, key=lambda candidate: (candidate.bbox[1], candidate.bbox[0]))


def _select_maximal_filled_grid_cells(
    rectangles: list[BBox],
    outer_bbox: BBox,
) -> list[BBox]:
    """移除边缘细条、重复 Path 和同跨度半行底纹，保留最大单元格。"""

    outer_width = outer_bbox[2] - outer_bbox[0]
    outer_height = outer_bbox[3] - outer_bbox[1]
    outer_area = _bbox_area(outer_bbox)
    x_tolerance = 0.01 * outer_width
    y_tolerance = 0.02 * outer_height
    nested_rectangles = [
        rectangle
        for rectangle in rectangles
        if rectangle != outer_bbox
        and _bbox_is_contained_with_tolerance(
            rectangle,
            outer_bbox,
            x_tolerance,
            y_tolerance,
        )
        and _bbox_area(rectangle) <= 0.8 * outer_area
        and rectangle[2] - rectangle[0] >= 0.1 * outer_width
        and rectangle[3] - rectangle[1] >= 0.06 * outer_height
    ]
    output: list[BBox] = []
    for rectangle in sorted(
        nested_rectangles,
        key=lambda bbox: (-_bbox_area(bbox), bbox[1], bbox[0]),
    ):
        if any(
            rectangle != other
            and _bbox_is_contained_with_tolerance(
                rectangle,
                other,
                x_tolerance,
                y_tolerance,
            )
            and _bbox_area(other) >= 1.08 * _bbox_area(rectangle)
            and _bbox_area(other) <= 0.8 * outer_area
            and abs((rectangle[2] - rectangle[0]) - (other[2] - other[0])) <= 0.01 * outer_width
            for other in nested_rectangles
        ):
            continue
        if any(_bbox_overlap_in_smaller(rectangle, accepted) >= 0.95 for accepted in output):
            continue
        output.append(rectangle)
    return sorted(output, key=lambda bbox: (bbox[1], bbox[0], bbox[3], bbox[2]))


def _bbox_is_contained_with_tolerance(
    inner_bbox: BBox,
    outer_bbox: BBox,
    x_tolerance: float,
    y_tolerance: float,
) -> bool:
    """按横纵独立容差判断一个矩形是否完整位于另一个矩形内。"""

    return (
        inner_bbox[0] >= outer_bbox[0] - x_tolerance
        and inner_bbox[1] >= outer_bbox[1] - y_tolerance
        and inner_bbox[2] <= outer_bbox[2] + x_tolerance
        and inner_bbox[3] <= outer_bbox[3] + y_tolerance
    )


def _group_filled_grid_cells_into_bands(
    cells: list[BBox],
    outer_bbox: BBox,
) -> list[list[BBox]]:
    """按相近上下边界把最大填充单元格聚成水平行带。"""

    y_tolerance = 0.02 * (outer_bbox[3] - outer_bbox[1])
    bands: list[list[BBox]] = []
    for cell in sorted(cells, key=lambda bbox: (bbox[1], bbox[0], bbox[3])):
        target = next(
            (band for band in bands if abs(cell[1] - band[0][1]) <= y_tolerance and abs(cell[3] - band[0][3]) <= y_tolerance),
            None,
        )
        if target is None:
            bands.append([cell])
        else:
            target.append(cell)
    return bands


def _filled_grid_band_covers_outer_width(
    band: list[BBox],
    outer_bbox: BBox,
) -> bool:
    """校验单个填充行带的单元格数量、横向覆盖、间隙和端点。"""

    if len(band) < 2:
        return False
    outer_width = outer_bbox[2] - outer_bbox[0]
    segments = sorted((cell[0], cell[2]) for cell in band)
    current_left, current_right = segments[0]
    covered_width = 0.0
    maximum_gap = 0.0
    for left, right in segments[1:]:
        if left <= current_right:
            current_right = max(current_right, right)
            continue
        covered_width += current_right - current_left
        maximum_gap = max(maximum_gap, left - current_right)
        current_left, current_right = left, right
    covered_width += current_right - current_left
    return (
        covered_width >= 0.9 * outer_width
        and maximum_gap <= 0.01 * outer_width
        and abs(min(cell[0] for cell in band) - outer_bbox[0]) <= 0.02 * outer_width
        and abs(max(cell[2] for cell in band) - outer_bbox[2]) <= 0.02 * outer_width
    )
