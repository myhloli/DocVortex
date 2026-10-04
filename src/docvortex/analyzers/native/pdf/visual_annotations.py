"""Strong rules caption/footnote for identifying independent visual patches and constructing local reading areas."""

from __future__ import annotations

import re
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal


from .._shared.xycut import sort_entries
from ....schema import BBox

from .annotation_text import (
    _normalize_annotation_text,
    _is_strong_caption_text,
    _caption_identifier,
)
from .geometry import (
    _bbox_axis_overlap_ratio,
    _bbox_center_x,
    _bbox_center_y,
    _bbox_union_many,
    _coerce_bbox,
    _rotate_bbox_to_upright,
)


_VISUAL_BLOCK_TYPES = {"image", "table", "code"}
_CAPTION_MAX_GAP_IN_LINE_HEIGHTS = 4.0
_FOOTNOTE_MAX_GAP_IN_LINE_HEIGHTS = 6.0
_MIN_PROJECTION_OVERLAP = 0.8
_MIN_ANNOTATION_COVERAGE = 0.65
_COMPONENT_COVERAGE_GAIN = 0.15
_CROSS_LANE_CAPTION_ROW_TOP_TOLERANCE = 0.25
_CROSS_LANE_CAPTION_MIN_LINE_HEIGHT_RATIO = 0.85
_CROSS_LANE_CAPTION_MAX_LINE_HEIGHT_RATIO = 1.15
_TABLE_FOOTNOTE_CONTINUATION_MAX_GAP = 1.5
_TABLE_FOOTNOTE_FIRST_INDENT_MIN = 0.5
_TABLE_FOOTNOTE_FIRST_INDENT_MAX = 2.0
_TABLE_FOOTNOTE_FOLLOWING_INDENT_TOLERANCE = 0.5
_TABLE_FOOTNOTE_LANE_TOLERANCE = 0.5
_TABLE_FOOTNOTE_MIN_LINE_HEIGHT_RATIO = 0.75
_TABLE_FOOTNOTE_MAX_LINE_HEIGHT_RATIO = 1.25

_FOOTNOTE_RE = re.compile(
    r"^\s*(?:source(?:s|\(s\))?|data\s+source|note(?:s|\(s\))?"
    r"|资料来源|数据来源|来源|注|备注)\s*[:：]\s*\S",
    re.IGNORECASE | re.DOTALL,
)
_Direction = Literal["above", "below", "left", "right"]
_AnnotationKind = Literal["caption", "footnote"]
_TextBlockGroupMerger = Callable[
    [list[dict[str, Any]], list[int]],
    dict[str, Any],
]


@dataclass(frozen=True)
class _VisualParent:
    """Record single visual chunks or just for associated multi-picture components."""

    member_indices: tuple[int, ...]
    angle: int
    local_bbox: BBox
    # Digital grid evidence only participates in parent object selection and does not participate in the public output schema.
    native_numeric_grid: bool = False


@dataclass(frozen=True)
class _AnnotationRelation:
    """Record the orientation and normalized geometric cost between the annotation and the candidate parent patch."""

    parent: _VisualParent
    direction: _Direction
    normalized_gap: float
    projection_overlap: float
    annotation_coverage: float
    center_offset: float


def _is_strong_footnote_text(text: str) -> bool:
    """Determines whether the text begins with a source or comment strong tag with a colon."""

    normalized = _normalize_annotation_text(text)
    # The abbreviated explanation may be combined with the source; the explanation itself must have an equal sign, and the source must still be clearly marked.
    definition_with_source = re.match(
        r"^[A-Za-z][A-Za-z0-9 -]{0,30}\s*=\s*\S.+?\s+Source(?:s)?\s*:\s*\S", normalized, re.I | re.S
    )
    reference_source = re.fullmatch(
        r"\(?[A-Z][A-Za-z’'-]+\s+(?:(?:and|&)\s+[A-Z][A-Za-z’'-]+\s+|et\s+al\.?\s+)?[12]\d{3}\)?\.?", normalized
    )
    adapted_source = re.match(r"^(?:table|figure)\s+(?:adapted|reprinted|reproduced)\s+from\s+\S", normalized, re.I)
    return bool(_FOOTNOTE_RE.match(normalized) or definition_with_source or reference_source or adapted_source)


def _block_angle(block: dict[str, Any]) -> int:
    """Normalize block angles to four orthogonal directions."""

    return int(block.get("angle", 0) or 0) % 360


def _block_local_bbox(
    block: dict[str, Any],
    page_size: tuple[float, float],
    angle: int | None = None,
) -> BBox | None:
    """Reads a valid bbox and converts it to local coordinates specifying the text direction."""

    bbox = _coerce_bbox(block.get("bbox"))
    if bbox is None:
        return None
    return _rotate_bbox_to_upright(
        bbox,
        page_size,
        _block_angle(block) if angle is None else angle,
    )


def _block_median_line_height(
    block: dict[str, Any],
    page_size: tuple[float, float],
) -> float:
    """Priority is given to using the native line height, and when missing, it is conservatively estimated based on the local block height and the number of line boxes."""

    heights = [
        float(height) for height in block.get("_line_heights", []) if isinstance(height, (int, float)) and float(height) > 0
    ]
    if heights:
        return statistics.median(heights)
    local_bbox = _block_local_bbox(block, page_size)
    if local_bbox is None:
        return 1.0
    local_rows = block.get("_local_line_bboxes")
    row_count = len(local_rows) if isinstance(local_rows, list) and local_rows else 1
    return max(1.0, (local_bbox[3] - local_bbox[1]) / row_count)


def _collect_annotation_candidates(
    blocks: list[dict[str, Any]],
) -> dict[int, _AnnotationKind]:
    """Collect visual binding candidates from stand-alone text and pre-classified caption/footnote."""

    output: dict[int, _AnnotationKind] = {}
    for index, block in enumerate(blocks):
        block_type = block.get("type")
        if block_type in {"caption", "footnote"}:
            output[index] = block_type
            continue
        if block_type not in {"text", "paragraph_title"} or not isinstance(block.get("content"), str):
            continue
        content = str(block["content"])
        if _is_strong_caption_text(content):
            output[index] = "caption"
        elif block_type == "text" and _is_strong_footnote_text(content):
            output[index] = "footnote"
        elif block_type == "text" and _is_adjacent_unlabelled_italic_caption(index, blocks):
            output[index] = "caption"
    # Explanations of the same font size that are close to strong source tags are candidates first, otherwise the explanation line will be regarded as a blocker and the entire annotation band cannot be bound.
    for anchor_index, kind in list(output.items()):
        anchor = blocks[anchor_index]
        if kind != "footnote" or _block_angle(anchor) != 0 or not _is_strong_footnote_text(str(anchor.get("content", ""))):
            continue
        height = _block_median_line_height(anchor, (0, 0))
        bounds = _coerce_bbox(anchor.get("bbox"))
        if bounds is None:
            continue
        images = [
            box
            for block in blocks
            if block.get("type") == "image"
            and (box := _coerce_bbox(block.get("bbox"))) is not None
            and 0 <= bounds[1] - box[3] <= 6 * height
            and box[0] - 0.25 * (box[2] - box[0]) <= bounds[0] <= box[2]
        ]
        if not images:
            continue
        image = max(images, key=lambda box: box[3])
        lower = bounds[1]
        preceding = sorted(
            (
                (index, box)
                for index, block in enumerate(blocks)
                if index not in output
                and block.get("type") == "text"
                and _block_angle(block) == 0
                and (box := _coerce_bbox(block.get("bbox"))) is not None
                and image[3] - 0.25 * height <= box[1]
                and box[3] <= lower + 0.25 * height
            ),
            key=lambda item: item[1][3],
            reverse=True,
        )
        for index, box in preceding:
            candidate = blocks[index]
            if (
                lower - box[3] > 1.5 * height
                or abs(box[0] - bounds[0]) > height
                or not re.match(r"^[A-Za-z][A-Za-z0-9 -]{0,30}\s*(?:=|:)\s*\S", str(candidate.get("content", "")))
                or not 0.85 * height <= _block_median_line_height(candidate, (0, 0)) <= 1.15 * height
                or box[2] > image[2] + 0.25 * (image[2] - image[0])
                or not _table_footnote_fonts_are_compatible(anchor, candidate)
                or _is_strong_caption_text(str(candidate.get("content", "")))
            ):
                break
            output[index] = "footnote"
            lower = box[1]
    return output


def _is_adjacent_unlabelled_italic_caption(index: int, blocks: list[dict[str, Any]]) -> bool:
    """Unnumbered figure captions need to be supported by a single line of italics, close to the larger figure, and a blank space in the subsequent text. They cannot be claimed based on short sentences."""
    block = blocks[index]
    box = _coerce_bbox(block.get("bbox"))
    fonts = block.get("_font_signatures")
    rows = block.get("_local_line_bboxes")
    text = str(block.get("content", ""))
    if (
        box is None
        or _block_angle(block) != 0
        or not isinstance(fonts, (set, frozenset))
        or not fonts
        or not all(isinstance(font, tuple) and len(font) == 2 and isinstance(font[1], int) and font[1] & 64 for font in fonts)
        or not isinstance(rows, list)
        or len(rows) != 1
        or not 4 <= len(re.findall(r"[A-Za-z]+", text)) <= 24
    ):
        return False
    em = _block_median_line_height(block, (0, 0))
    images = [
        image
        for image in blocks
        if image.get("type") == "image"
        and (bounds := _coerce_bbox(image.get("bbox"))) is not None
        and bounds[2] - bounds[0] >= 10 * em
        and bounds[3] - bounds[1] >= 8 * em
        and 0 <= box[1] - bounds[3] <= 0.75 * em
        and abs(box[0] - bounds[0]) <= 0.75 * em
        and box[2] <= bounds[2] + 0.5 * em
    ]
    if len(images) != 1:
        return False
    following = [
        bounds
        for other in blocks
        if other is not block
        and other.get("type") == "text"
        and (bounds := _coerce_bbox(other.get("bbox"))) is not None
        and bounds[1] >= box[3]
        and abs(bounds[0] - box[0]) <= em
        and len(re.findall(r"[A-Za-z]+", str(other.get("content", "")))) >= 15
    ]
    return bool(following and min(bounds[1] for bounds in following) - box[3] >= 1.5 * em)


def _coerce_lane_interval(value: object) -> tuple[float, float] | None:
    """Read internal column ranges, rejecting missing, reversed, or non-numeric metadata."""

    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        start, end = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    if end <= start:
        return None
    return start, end


def _block_first_local_row_bbox(
    block: dict[str, Any],
    page_size: tuple[float, float],
    angle: int,
) -> BBox | None:
    """Returns the first text line box of the block in local coordinates in the specified direction."""

    rows = block.get("_local_line_bboxes")
    local_rows = [bbox for row in rows if (bbox := _coerce_bbox(row)) is not None] if isinstance(rows, list) else []
    if local_rows:
        return min(local_rows, key=lambda bbox: (bbox[1], bbox[0]))
    return _block_local_bbox(block, page_size, angle)


def _blocks_share_table_footnote_lane(
    anchor: dict[str, Any],
    candidate: dict[str, Any],
    line_height: float,
) -> bool:
    """It is required that the table note anchor and the continuation block be from the same inner band and span level."""

    if anchor.get("_lane_is_span") != candidate.get("_lane_is_span"):
        return False
    anchor_interval = _coerce_lane_interval(anchor.get("_lane_interval"))
    candidate_interval = _coerce_lane_interval(candidate.get("_lane_interval"))
    if anchor_interval is None or candidate_interval is None:
        return False
    tolerance = _TABLE_FOOTNOTE_LANE_TOLERANCE * line_height
    return (
        abs(anchor_interval[0] - candidate_interval[0]) <= tolerance
        and abs(anchor_interval[1] - candidate_interval[1]) <= tolerance
    )


def _block_overlaps_table_footnote_lane(
    block: dict[str, Any],
    lane_interval: tuple[float, float],
    page_size: tuple[float, float],
    angle: int,
) -> bool:
    """Determine whether any block crosses the table and note column band to ensure that the collection process does not cross obstacles."""

    local_bbox = _block_local_bbox(block, page_size, angle)
    if local_bbox is None:
        return False
    overlap = max(
        0.0,
        min(local_bbox[2], lane_interval[1]) - max(local_bbox[0], lane_interval[0]),
    )
    shorter_width = max(
        0.1,
        min(
            local_bbox[2] - local_bbox[0],
            lane_interval[1] - lane_interval[0],
        ),
    )
    return overlap / shorter_width >= 0.5


def _table_footnote_fonts_are_compatible(
    previous: dict[str, Any],
    candidate: dict[str, Any],
) -> bool:
    """Reject continuation blocks only if both sides have reliable font information and are completely disjoint."""

    previous_fonts = previous.get("_font_signatures")
    candidate_fonts = candidate.get("_font_signatures")
    return not (
        isinstance(previous_fonts, set)
        and isinstance(candidate_fonts, set)
        and previous_fonts
        and candidate_fonts
        and previous_fonts.isdisjoint(candidate_fonts)
    )


def _table_footnote_candidate_fits_parent(
    candidate_bbox: BBox,
    parent_bbox: BBox,
    line_height: float,
) -> bool:
    """Requires continuation blocks to be positioned below the parent table and maintain existing horizontal projection coverage standards."""

    if _bbox_center_y(candidate_bbox) < _bbox_center_y(parent_bbox):
        return False
    projection_overlap, annotation_coverage, _center_offset = _axis_overlap_metrics(
        candidate_bbox,
        parent_bbox,
        axis="x",
        float_margin=line_height,
    )
    return projection_overlap >= _MIN_PROJECTION_OVERLAP and annotation_coverage >= _MIN_ANNOTATION_COVERAGE


def _table_footnote_continuation_is_compatible(
    anchor: dict[str, Any],
    previous: dict[str, Any],
    candidate: dict[str, Any],
    parent_bbox: BBox,
    page_size: tuple[float, float],
    angle: int,
    continuation_left: float | None,
) -> tuple[bool, float | None]:
    """Acknowledge individual table note continuation blocks by banding, font, line height, headroom, and hanging indent."""

    previous_bbox = _block_local_bbox(previous, page_size, angle)
    candidate_bbox = _block_local_bbox(candidate, page_size, angle)
    anchor_first_row = _block_first_local_row_bbox(anchor, page_size, angle)
    candidate_first_row = _block_first_local_row_bbox(candidate, page_size, angle)
    if previous_bbox is None or candidate_bbox is None or anchor_first_row is None or candidate_first_row is None:
        return False, continuation_left

    previous_height = _block_median_line_height(previous, page_size)
    candidate_height = _block_median_line_height(candidate, page_size)
    if previous_height <= 0 or candidate_height <= 0:
        return False, continuation_left
    height_ratio = candidate_height / previous_height
    pair_height = statistics.median((previous_height, candidate_height))
    vertical_gap = max(0.0, candidate_bbox[1] - previous_bbox[3])
    if (
        not _TABLE_FOOTNOTE_MIN_LINE_HEIGHT_RATIO <= height_ratio <= _TABLE_FOOTNOTE_MAX_LINE_HEIGHT_RATIO
        or vertical_gap > _TABLE_FOOTNOTE_CONTINUATION_MAX_GAP * pair_height
        or not _blocks_share_table_footnote_lane(anchor, candidate, pair_height)
        or not _table_footnote_fonts_are_compatible(previous, candidate)
        or not _table_footnote_candidate_fits_parent(
            candidate_bbox,
            parent_bbox,
            pair_height,
        )
    ):
        return False, continuation_left

    candidate_left = candidate_first_row[0]
    if continuation_left is None:
        indent = candidate_left - anchor_first_row[0]
        if not (_TABLE_FOOTNOTE_FIRST_INDENT_MIN * pair_height <= indent <= _TABLE_FOOTNOTE_FIRST_INDENT_MAX * pair_height):
            return False, continuation_left
        return True, candidate_left
    if abs(candidate_left - continuation_left) > _TABLE_FOOTNOTE_FOLLOWING_INDENT_TOLERANCE * pair_height:
        return False, continuation_left
    return True, continuation_left


def _collect_table_footnote_continuation_indices(
    anchor_index: int,
    relation: _AnnotationRelation,
    blocks: list[dict[str, Any]],
    page_size: tuple[float, float],
    annotation_indices: set[int],
    consumed_indices: set[int],
) -> list[int]:
    """Collect hanging indented text in the same column continuously from the table note anchor point downwards, without crossing the first barrier."""

    anchor = blocks[anchor_index]
    angle = relation.parent.angle
    anchor_bbox = _block_local_bbox(anchor, page_size, angle)
    lane_interval = _coerce_lane_interval(anchor.get("_lane_interval"))
    if anchor_bbox is None or lane_interval is None:
        return []

    ordered_indices = sorted(
        (
            index
            for index, block in enumerate(blocks)
            if index != anchor_index
            and index not in relation.parent.member_indices
            and index not in consumed_indices
            and _block_angle(block) == angle
            and (local_bbox := _block_local_bbox(block, page_size, angle)) is not None
            and _bbox_center_y(local_bbox) > _bbox_center_y(anchor_bbox)
            and _block_overlaps_table_footnote_lane(
                block,
                lane_interval,
                page_size,
                angle,
            )
        ),
        key=lambda index: (
            _block_local_bbox(blocks[index], page_size, angle)[1],
            _block_local_bbox(blocks[index], page_size, angle)[0],
            index,
        ),
    )

    output: list[int] = []
    previous = anchor
    continuation_left: float | None = None
    for index in ordered_indices:
        candidate = blocks[index]
        if index in annotation_indices or candidate.get("type") != "text":
            break
        compatible, continuation_left = _table_footnote_continuation_is_compatible(
            anchor,
            previous,
            candidate,
            relation.parent.local_bbox,
            page_size,
            angle,
            continuation_left,
        )
        if not compatible:
            break
        output.append(index)
        previous = candidate
    return output


def _merge_table_footnote_continuations(
    blocks: list[dict[str, Any]],
    candidates: dict[int, _AnnotationKind],
    assignments: dict[int, _AnnotationRelation],
    page_size: tuple[float, float],
    merge_text_block_group: _TextBlockGroupMerger | None,
) -> set[int]:
    """Merges the strongly marked footnote of a bound single table and its in-page continuation block, and returns the consumed index."""

    if merge_text_block_group is None:
        return set()
    annotation_indices = set(candidates)
    consumed_indices: set[int] = set()
    for anchor_index in sorted(
        assignments,
        key=lambda index: _annotation_local_sort_key(index, blocks, page_size),
    ):
        relation = assignments[anchor_index]
        parent_indices = relation.parent.member_indices
        anchor = blocks[anchor_index]
        if (
            candidates.get(anchor_index) != "footnote"
            or anchor.get("_table_annotation_complete") is True
            or anchor.get("type") != "text"
            or not isinstance(anchor.get("content"), str)
            or not _is_strong_footnote_text(str(anchor["content"]))
            or relation.direction != "below"
            or len(parent_indices) != 1
            or blocks[parent_indices[0]].get("type") != "table"
        ):
            continue
        continuation_indices = _collect_table_footnote_continuation_indices(
            anchor_index,
            relation,
            blocks,
            page_size,
            annotation_indices,
            consumed_indices,
        )
        if not continuation_indices:
            continue
        blocks[anchor_index] = merge_text_block_group(
            blocks,
            [anchor_index, *continuation_indices],
        )
        consumed_indices.update(continuation_indices)
    return consumed_indices


def _axis_overlap_metrics(
    annotation_bbox: BBox,
    parent_bbox: BBox,
    *,
    axis: Literal["x", "y"],
    float_margin: float,
) -> tuple[float, float, float]:
    """Shorter projections, annotation coverage and center offsets are calculated after allowing one line of height float on the orthogonal axis."""

    if axis == "x":
        annotation_start, annotation_end = annotation_bbox[0], annotation_bbox[2]
        parent_start, parent_end = parent_bbox[0], parent_bbox[2]
    else:
        annotation_start, annotation_end = annotation_bbox[1], annotation_bbox[3]
        parent_start, parent_end = parent_bbox[1], parent_bbox[3]
    expanded_parent_start = parent_start - float_margin
    expanded_parent_end = parent_end + float_margin
    overlap = max(
        0.0,
        min(annotation_end, expanded_parent_end) - max(annotation_start, expanded_parent_start),
    )
    annotation_length = max(0.1, annotation_end - annotation_start)
    parent_length = max(0.1, expanded_parent_end - expanded_parent_start)
    projection_overlap = overlap / min(annotation_length, parent_length)
    annotation_coverage = overlap / annotation_length
    center_offset = abs((annotation_start + annotation_end) / 2.0 - (parent_start + parent_end) / 2.0) / max(
        annotation_length, parent_end - parent_start, 0.1
    )
    return projection_overlap, annotation_coverage, center_offset


def _direction_relation(
    parent: _VisualParent,
    annotation_bbox: BBox,
    line_height: float,
    direction: _Direction,
    max_gap_in_line_heights: float,
) -> _AnnotationRelation | None:
    """Check edge distance, depth, and orthogonal projection constraints in one direction."""

    parent_bbox = parent.local_bbox
    if direction == "above":
        if _bbox_center_y(annotation_bbox) > _bbox_center_y(parent_bbox):
            return None
        signed_gap = parent_bbox[1] - annotation_bbox[3]
        projection_axis: Literal["x", "y"] = "x"
    elif direction == "below":
        if _bbox_center_y(annotation_bbox) < _bbox_center_y(parent_bbox):
            return None
        signed_gap = annotation_bbox[1] - parent_bbox[3]
        projection_axis = "x"
    elif direction == "left":
        if _bbox_center_x(annotation_bbox) > _bbox_center_x(parent_bbox):
            return None
        signed_gap = parent_bbox[0] - annotation_bbox[2]
        projection_axis = "y"
    else:
        if _bbox_center_x(annotation_bbox) < _bbox_center_x(parent_bbox):
            return None
        signed_gap = annotation_bbox[0] - parent_bbox[2]
        projection_axis = "y"
    gap = max(0.0, signed_gap)
    penetration = max(0.0, -signed_gap)
    if gap > max_gap_in_line_heights * line_height or penetration > line_height:
        return None
    projection_overlap, annotation_coverage, center_offset = _axis_overlap_metrics(
        annotation_bbox,
        parent_bbox,
        axis=projection_axis,
        float_margin=line_height,
    )
    centered_wide_caption = projection_axis == "x" and center_offset <= 0.08 and projection_overlap >= 0.95
    inset_parent_caption = (
        projection_axis == "x"
        and parent_bbox[2] - parent_bbox[0] >= 1.5 * (annotation_bbox[2] - annotation_bbox[0])
        and gap <= 2 * line_height
        and annotation_coverage >= 0.5
    )
    if projection_overlap < (0.5 if inset_parent_caption else _MIN_PROJECTION_OVERLAP) or (
        annotation_coverage < _MIN_ANNOTATION_COVERAGE and not centered_wide_caption
    ):
        return None
    return _AnnotationRelation(
        parent=parent,
        direction=direction,
        normalized_gap=gap / line_height,
        projection_overlap=projection_overlap,
        annotation_coverage=annotation_coverage,
        center_offset=center_offset,
    )


def _best_parent_relation(
    parent: _VisualParent,
    annotation_bbox: BBox,
    line_height: float,
    kind: _AnnotationKind,
) -> _AnnotationRelation | None:
    """Select the legal annotation direction with the least geometric cost for a single parent block."""

    directions: tuple[_Direction, ...]
    if kind == "footnote":
        directions = ("below",)
        max_gap = _FOOTNOTE_MAX_GAP_IN_LINE_HEIGHTS
    else:
        directions = ("above", "below", "left", "right")
        max_gap = _CAPTION_MAX_GAP_IN_LINE_HEIGHTS
    relations = [
        relation
        for direction in directions
        if (
            relation := _direction_relation(
                parent,
                annotation_bbox,
                line_height,
                direction,
                max_gap,
            )
        )
        is not None
    ]
    if not relations:
        return None
    return min(
        relations,
        key=lambda relation: (
            relation.normalized_gap,
            -relation.annotation_coverage,
            -relation.projection_overlap,
            relation.center_offset,
        ),
    )


def _image_blocks_form_component(
    first_bbox: BBox,
    second_bbox: BBox,
    line_height: float,
) -> bool:
    """Determine whether two images form adjacent panels with small headroom and sufficient orthogonal projection."""

    horizontal_gap = max(first_bbox[0] - second_bbox[2], second_bbox[0] - first_bbox[2], 0.0)
    vertical_gap = max(first_bbox[1] - second_bbox[3], second_bbox[1] - first_bbox[3], 0.0)
    x_overlap = max(0.0, min(first_bbox[2], second_bbox[2]) - max(first_bbox[0], second_bbox[0]))
    y_overlap = max(0.0, min(first_bbox[3], second_bbox[3]) - max(first_bbox[1], second_bbox[1]))
    min_width = max(0.1, min(first_bbox[2] - first_bbox[0], second_bbox[2] - second_bbox[0]))
    min_height = max(0.1, min(first_bbox[3] - first_bbox[1], second_bbox[3] - second_bbox[1]))
    return (horizontal_gap <= 2.0 * line_height and y_overlap / min_height >= 0.5) or (
        vertical_gap <= 2.0 * line_height and x_overlap / min_width >= 0.5
    )


def _build_visual_parents(
    blocks: list[dict[str, Any]],
    page_size: tuple[float, float],
    component_line_height: float,
) -> list[_VisualParent]:
    """Construct monovisual patch parent candidates and supplement adjacent images with connected components only for association."""

    parents: list[_VisualParent] = []
    image_indices_by_angle: dict[int, list[int]] = {}
    local_bboxes: dict[int, BBox] = {}
    for index, block in enumerate(blocks):
        if block.get("type") not in _VISUAL_BLOCK_TYPES:
            continue
        angle = _block_angle(block)
        local_bbox = _block_local_bbox(block, page_size, angle)
        if local_bbox is None:
            continue
        local_bboxes[index] = local_bbox
        parents.append(_VisualParent((index,), angle, local_bbox, bool(block.get("_native_numeric_grid"))))
        if block.get("type") == "image":
            image_indices_by_angle.setdefault(angle, []).append(index)

    for angle, image_indices in image_indices_by_angle.items():
        remaining = set(image_indices)
        while remaining:
            seed = min(remaining)
            component = {seed}
            remaining.remove(seed)
            changed = True
            while changed:
                changed = False
                for candidate in list(remaining):
                    if any(
                        _image_blocks_form_component(
                            local_bboxes[member],
                            local_bboxes[candidate],
                            component_line_height,
                        )
                        for member in component
                    ):
                        component.add(candidate)
                        remaining.remove(candidate)
                        changed = True
            if len(component) < 2:
                continue
            member_indices = tuple(sorted(component))
            parents.append(
                _VisualParent(
                    member_indices,
                    angle,
                    _bbox_union_many([local_bboxes[index] for index in member_indices]),
                )
            )
    return parents


def _add_caption_supported_table_panel_parents(
    blocks: list[dict[str, Any]],
    page_size: tuple[float, float],
    parents: list[_VisualParent],
    candidates: dict[int, _AnnotationKind],
) -> None:
    """Double tables of the same height, respective a/b instructions and hurdle general table questions jointly confirm the panel group; keep the two table bodies and the general description appearing once each."""
    tables = [
        parent
        for parent in parents
        if len(parent.member_indices) == 1 and blocks[parent.member_indices[0]].get("type") == "table"
    ]
    for caption_index, kind in list(candidates.items()):
        caption = blocks[caption_index]
        if kind != "caption" or not re.match(r"^\s*tab(?:le)?[.\s0-9]", str(caption.get("content", "")), re.I):
            continue
        height = _block_median_line_height(caption, page_size)
        angle = _block_angle(caption)
        caption_box = _block_local_bbox(caption, page_size, angle)
        if caption_box is None or height <= 0:
            continue
        nearby = sorted(
            [
                parent
                for parent in tables
                if parent.angle == angle
                and 0 <= caption_box[1] - parent.local_bbox[3] <= 4 * height
                and caption_box[0] - height <= parent.local_bbox[0]
                and parent.local_bbox[2] <= caption_box[2] + height
            ],
            key=lambda parent: parent.local_bbox[0],
        )
        if len(nearby) != 2:
            continue
        left, right = nearby
        a, b = left.local_bbox, right.local_bbox
        if (
            abs(a[1] - b[1]) > 0.5 * height
            or abs(a[3] - b[3]) > 0.5 * height
            or not 0 <= b[0] - a[2] <= 8 * height
            or not 0.7 <= (a[2] - a[0]) / max(b[2] - b[0], 1e-6) <= 1.43
        ):
            continue
        notes = []
        for parent, letter in zip(nearby, ("a", "b")):
            box = parent.local_bbox
            matches = [
                index
                for index, block in enumerate(blocks)
                if block.get("type") == "text"
                and _block_angle(block) == angle
                and re.match(rf"^\s*\({letter}\)\s+\S", str(block.get("content", "")))
                and (note_box := _block_local_bbox(block, page_size, angle)) is not None
                and 0 <= note_box[1] - box[3] <= 2 * height
                and note_box[3] <= caption_box[1] + 0.1 * height
                and box[0] - 0.25 * height <= note_box[0] < note_box[2] <= box[2] + 0.25 * height
                and 0.75 * height <= _block_median_line_height(block, page_size) <= 1.25 * height
            ]
            if len(matches) != 1:
                break
            notes.extend(matches)
        if len(notes) != 2:
            continue
        parent = _VisualParent(tuple(p.member_indices[0] for p in nearby), angle, _bbox_union_many([a, b]))
        relation = _direction_relation(parent, caption_box, height, "below", 4)
        if relation is None or _relation_has_intervening_block(
            relation, caption_index, caption_box, height, blocks, page_size, set(candidates) | set(notes)
        ):
            continue
        parents.append(parent)
        candidates.update(dict.fromkeys(notes, "caption"))


def _relation_has_intervening_block(
    relation: _AnnotationRelation,
    annotation_index: int,
    annotation_bbox: BBox,
    line_height: float,
    blocks: list[dict[str, Any]],
    page_size: tuple[float, float],
    annotation_indices: set[int],
) -> bool:
    """Check if the annotation is within a clear corridor between the parent block and the parent block, separated by text or another visual block."""

    if relation.normalized_gap <= 0:
        return False
    parent_bbox = relation.parent.local_bbox
    if relation.direction in {"above", "below"}:
        orth_start = max(annotation_bbox[0], parent_bbox[0]) - 0.25 * line_height
        orth_end = min(annotation_bbox[2], parent_bbox[2]) + 0.25 * line_height
        if relation.direction == "above":
            corridor = (orth_start, annotation_bbox[3], orth_end, parent_bbox[1])
        else:
            corridor = (orth_start, parent_bbox[3], orth_end, annotation_bbox[1])
    else:
        orth_start = max(annotation_bbox[1], parent_bbox[1]) - 0.25 * line_height
        orth_end = min(annotation_bbox[3], parent_bbox[3]) + 0.25 * line_height
        if relation.direction == "left":
            corridor = (annotation_bbox[2], orth_start, parent_bbox[0], orth_end)
        else:
            corridor = (parent_bbox[2], orth_start, annotation_bbox[0], orth_end)
    if corridor[2] <= corridor[0] or corridor[3] <= corridor[1]:
        return False
    ignored_indices = {
        annotation_index,
        *relation.parent.member_indices,
        *annotation_indices,
    }
    for index, block in enumerate(blocks):
        if index in ignored_indices or _block_angle(block) != relation.parent.angle:
            continue
        bbox = _block_local_bbox(block, page_size, relation.parent.angle)
        if bbox is None:
            continue
        if min(bbox[2], corridor[2]) > max(bbox[0], corridor[0]) and min(bbox[3], corridor[3]) > max(bbox[1], corridor[1]):
            return True
    return False


def _component_relation_is_materially_better(
    relation: _AnnotationRelation,
    single_relations: list[_AnnotationRelation],
) -> bool:
    """Allow components to override single-image parent blocks only if image union significantly improves annotation coverage."""

    if len(relation.parent.member_indices) < 2:
        return True
    member_indices = set(relation.parent.member_indices)
    comparable = [
        single
        for single in single_relations
        if single.direction == relation.direction and single.parent.member_indices[0] in member_indices
    ]
    if not comparable:
        return True
    best_single_coverage = max(single.annotation_coverage for single in comparable)
    return relation.annotation_coverage >= best_single_coverage + _COMPONENT_COVERAGE_GAIN


def _choose_annotation_relation(
    annotation_index: int,
    kind: _AnnotationKind,
    blocks: list[dict[str, Any]],
    page_size: tuple[float, float],
    parents: list[_VisualParent],
    annotation_indices: set[int],
) -> _AnnotationRelation | None:
    """First filter the space, component income and blocking relationship, and then select the parent block by distance, coverage and center."""

    annotation = blocks[annotation_index]
    angle = _block_angle(annotation)
    annotation_bbox = _block_local_bbox(annotation, page_size, angle)
    if annotation_bbox is None:
        return None
    line_height = _block_median_line_height(annotation, page_size)
    relations = [
        relation
        for parent in parents
        if parent.angle == angle
        and (
            relation := _best_parent_relation(
                parent,
                annotation_bbox,
                line_height,
                kind,
            )
        )
        is not None
    ]
    if kind == "caption" and not relations and _is_strong_caption_text(str(annotation.get("content", ""))):
        # Numbered figure titles can be hung in the center figure body along the outside of the text column; only single figures with a single nearest neighbor and substantial horizontal coverage will be accepted.
        outdented = []
        for parent in parents:
            if (
                parent.angle != angle
                or len(parent.member_indices) != 1
                or blocks[parent.member_indices[0]].get("type") != "image"
            ):
                continue
            image = parent.local_bbox
            direction = "above" if annotation_bbox[3] <= image[1] else "below"
            gap = image[1] - annotation_bbox[3] if direction == "above" else annotation_bbox[1] - image[3]
            projection, coverage, offset = _axis_overlap_metrics(annotation_bbox, image, axis="x", float_margin=line_height)
            if (
                0 <= gap <= 3 * line_height
                and projection >= 0.55
                and coverage >= 0.55
                and annotation_bbox[0] >= image[0] - 0.45 * (image[2] - image[0])
                and annotation_bbox[2] <= image[2] + 0.45 * (image[2] - image[0])
            ):
                outdented.append(_AnnotationRelation(parent, direction, gap / line_height, projection, coverage, offset))
        if len(outdented) == 1:
            relations.extend(outdented)
    if kind == "footnote":
        # The figure title provides the area it belongs to, and short sources are allowed to have limited overhang on the left side of the figure body; text obstruction is still rejected by the unified corridor inspection.
        for parent in parents:
            if (
                parent.angle != angle
                or len(parent.member_indices) != 1
                or blocks[parent.member_indices[0]].get("type") != "image"
            ):
                continue
            image = parent.local_bbox
            image_width = image[2] - image[0]
            if not (
                0 <= annotation_bbox[1] - image[3] <= 6 * line_height
                and image[0] - 0.25 * image_width <= annotation_bbox[0] <= image[2]
                and annotation_bbox[2] <= image[2] + 0.25 * image_width
            ):
                continue
            supported = any(
                _is_strong_caption_text(str(blocks[index].get("content", "")))
                and _block_angle(blocks[index]) == angle
                and (caption_box := _block_local_bbox(blocks[index], page_size, angle)) is not None
                and _best_parent_relation(parent, caption_box, _block_median_line_height(blocks[index], page_size), "caption")
                is not None
                for index in annotation_indices
            )
            if supported:
                relations.append(
                    _AnnotationRelation(parent, "below", (annotation_bbox[1] - image[3]) / line_height, 1.0, 1.0, 0.0)
                )
    single_relations = [relation for relation in relations if len(relation.parent.member_indices) == 1]
    relations = [
        relation
        for relation in relations
        if _component_relation_is_materially_better(relation, single_relations)
        and not _relation_has_intervening_block(
            relation,
            annotation_index,
            annotation_bbox,
            line_height,
            blocks,
            page_size,
            annotation_indices,
        )
    ]
    if not relations:
        return None
    # When the figure title clearly describes the graph and the real image below is legal, the repeating number grid cannot preempt the parent relationship at a closer distance.
    if kind == "caption" and re.search(r"\b(?:graph|chart|plot)\b", str(annotation.get("content", "")), re.I):
        alternatives = [
            relation for relation in relations if not relation.parent.native_numeric_grid and relation.direction == "above"
        ]
        if alternatives and any(relation.parent.native_numeric_grid for relation in relations):
            relations = alternatives
    return min(
        relations,
        key=lambda relation: (
            relation.normalized_gap,
            -relation.annotation_coverage,
            -relation.projection_overlap,
            relation.center_offset,
            len(relation.parent.member_indices),
            relation.parent.member_indices,
        ),
    )


def _caption_blocks_use_distinct_regular_lanes(
    anchor: dict[str, Any],
    candidate: dict[str, Any],
) -> bool:
    """Confirm that the two title blocks belong to common columns that do not overlap each other."""

    if anchor.get("_lane_is_span") is not False or candidate.get("_lane_is_span") is not False:
        return False
    anchor_interval = _coerce_lane_interval(anchor.get("_lane_interval"))
    candidate_interval = _coerce_lane_interval(candidate.get("_lane_interval"))
    if anchor_interval is None or candidate_interval is None:
        return False
    return anchor_interval[1] <= candidate_interval[0] or candidate_interval[1] <= anchor_interval[0]


def _caption_blocks_have_compatible_typography(
    anchor: dict[str, Any],
    candidate: dict[str, Any],
    page_size: tuple[float, float],
) -> bool:
    """Use line height and font intersection to confirm that cross-column title blocks are from the same typography hierarchy."""

    anchor_height = _block_median_line_height(anchor, page_size)
    candidate_height = _block_median_line_height(candidate, page_size)
    if anchor_height <= 0 or candidate_height <= 0:
        return False
    height_ratio = candidate_height / anchor_height
    if not (_CROSS_LANE_CAPTION_MIN_LINE_HEIGHT_RATIO <= height_ratio <= _CROSS_LANE_CAPTION_MAX_LINE_HEIGHT_RATIO):
        return False

    anchor_fonts = anchor.get("_font_signatures")
    candidate_fonts = candidate.get("_font_signatures")
    if not (
        isinstance(anchor_fonts, (set, frozenset))
        and isinstance(candidate_fonts, (set, frozenset))
        and anchor_fonts
        and candidate_fonts
    ):
        return False
    return not anchor_fonts.isdisjoint(candidate_fonts)


def _cross_lane_caption_companion_relation(
    anchor_index: int,
    candidate_index: int,
    anchor_relation: _AnnotationRelation,
    blocks: list[dict[str, Any]],
    page_size: tuple[float, float],
    parents: list[_VisualParent],
    annotation_indices: set[int],
) -> _AnnotationRelation | None:
    """Identify a cross-heading companion based solely on spacing, banding, and typography information."""

    if anchor_relation.direction not in {"above", "below"}:
        return None
    anchor = blocks[anchor_index]
    candidate = blocks[candidate_index]
    if candidate.get("type") != "text" or _block_angle(candidate) != anchor_relation.parent.angle:
        return None
    if not _caption_blocks_use_distinct_regular_lanes(anchor, candidate):
        return None
    if not _caption_blocks_have_compatible_typography(
        anchor,
        candidate,
        page_size,
    ):
        return None

    angle = anchor_relation.parent.angle
    anchor_first_row = _block_first_local_row_bbox(anchor, page_size, angle)
    candidate_first_row = _block_first_local_row_bbox(
        candidate,
        page_size,
        angle,
    )
    if anchor_first_row is None or candidate_first_row is None:
        return None
    pair_height = statistics.median(
        (
            _block_median_line_height(anchor, page_size),
            _block_median_line_height(candidate, page_size),
        )
    )
    if abs(anchor_first_row[1] - candidate_first_row[1]) > _CROSS_LANE_CAPTION_ROW_TOP_TOLERANCE * pair_height:
        return None

    relation = _choose_annotation_relation(
        candidate_index,
        "caption",
        blocks,
        page_size,
        parents,
        annotation_indices | {candidate_index},
    )
    if relation is None or relation.parent != anchor_relation.parent or relation.direction != anchor_relation.direction:
        return None
    return relation


def _expand_cross_lane_caption_assignments(
    blocks: list[dict[str, Any]],
    page_size: tuple[float, float],
    parents: list[_VisualParent],
    candidates: dict[int, _AnnotationKind],
    assignments: dict[int, _AnnotationRelation],
) -> dict[int, _AnnotationRelation]:
    """Starting from the original bound title, conservatively fill in the only hurdle space partner."""

    annotation_indices = set(candidates)
    proposals: dict[int, list[_AnnotationRelation]] = {}
    for anchor_index, anchor_relation in assignments.items():
        if candidates.get(anchor_index) != "caption":
            continue
        matches = [
            (index, relation)
            for index in range(len(blocks))
            if index not in annotation_indices
            and (
                relation := _cross_lane_caption_companion_relation(
                    anchor_index,
                    index,
                    anchor_relation,
                    blocks,
                    page_size,
                    parents,
                    annotation_indices,
                )
            )
            is not None
        ]
        if len(matches) != 1:
            continue
        index, relation = matches[0]
        proposals.setdefault(index, []).append(relation)

    output: dict[int, _AnnotationRelation] = {}
    for index, relations in proposals.items():
        # When a companion is claimed by multiple anchors at the same time, the ownership is not unique, and the text classification is conservatively maintained.
        if len(relations) == 1:
            output[index] = relations[0]
    return output


def _merge_parent_assignment_groups(
    assignments: dict[int, _AnnotationRelation],
) -> list[tuple[set[int], dict[int, _AnnotationRelation]]]:
    """Merge parent candidates of shared visual members to prevent components from repeatedly expanding the same body with a single image."""

    groups: list[tuple[set[int], dict[int, _AnnotationRelation]]] = []
    for annotation_index, relation in assignments.items():
        parent_indices = set(relation.parent.member_indices)
        overlapping = [index for index, (members, _relations) in enumerate(groups) if members & parent_indices]
        merged_members = set(parent_indices)
        merged_relations = {annotation_index: relation}
        for group_index in reversed(overlapping):
            members, relations = groups.pop(group_index)
            merged_members.update(members)
            merged_relations.update(relations)
        groups.append((merged_members, merged_relations))
    return groups


def _sort_visual_body_members(
    member_indices: set[int],
    blocks: list[dict[str, Any]],
    page_size: tuple[float, float],
) -> list[dict[str, Any]]:
    """Arrange visual subject members in a region within a common local direction with XYCut++."""

    if not member_indices:
        return []
    angle = _block_angle(blocks[min(member_indices)])
    proxies: list[dict[str, Any]] = []
    for index in sorted(member_indices):
        local_bbox = _block_local_bbox(blocks[index], page_size, angle)
        if local_bbox is None:
            continue
        proxies.append({"bbox": local_bbox, "_block": blocks[index]})
    return [proxy["_block"] for proxy in sort_entries(proxies)]


def _annotation_local_sort_key(
    index: int,
    blocks: list[dict[str, Any]],
    page_size: tuple[float, float],
) -> tuple[float, float, int]:
    """Provides stable sorting keys by local top and left coordinates in the annotation's own direction."""

    block = blocks[index]
    if "_legend_sort_band" in block:
        return (*block["_legend_sort_band"], index)
    local_bbox = _block_local_bbox(block, page_size)
    if local_bbox is None:
        return (float("inf"), float("inf"), index)
    return (local_bbox[1], local_bbox[0], index)


def _sort_annotation_indices_by_visual_rows(
    indices: list[int],
    blocks: list[dict[str, Any]],
    page_size: tuple[float, float],
    relations: dict[int, _AnnotationRelation],
) -> list[int]:
    """First, aggregate the first row of aligned title visual rows, and then sort them stably from left to right within the row."""

    positioned: list[tuple[int, BBox, float]] = []
    fallback: list[int] = []
    for index in indices:
        block = blocks[index]
        angle = _block_angle(block)
        first_row = _block_first_local_row_bbox(block, page_size, angle)
        if first_row is None:
            fallback.append(index)
            continue
        positioned.append(
            (
                index,
                first_row,
                _block_median_line_height(block, page_size),
            )
        )
    positioned.sort(key=lambda item: (item[1][1], item[1][0], item[0]))

    rows: list[list[tuple[int, BBox, float]]] = []
    for item in positioned:
        if rows:
            current_row = rows[-1]
            item_relation = relations.get(item[0])
            shares_cross_lane_row = (
                item_relation is not None
                and item_relation.direction in {"above", "below"}
                and all(
                    (member_relation := relations.get(member[0])) is not None
                    and member_relation.direction == item_relation.direction
                    and _caption_blocks_use_distinct_regular_lanes(
                        blocks[member[0]],
                        blocks[item[0]],
                    )
                    for member in current_row
                )
            )
            reference_top = statistics.median(member[1][1] for member in current_row)
            reference_height = statistics.median(member[2] for member in current_row)
            pair_height = statistics.median((reference_height, item[2]))
            if shares_cross_lane_row and abs(item[1][1] - reference_top) <= _CROSS_LANE_CAPTION_ROW_TOP_TOLERANCE * pair_height:
                current_row.append(item)
                continue
        rows.append([item])

    output: list[int] = []
    for row in rows:
        row.sort(key=lambda item: (item[1][0], item[1][1], item[0]))
        output.extend(item[0] for item in row)
    output.extend(
        sorted(
            fallback,
            key=lambda index: _annotation_local_sort_key(
                index,
                blocks,
                page_size,
            ),
        )
    )
    return output


def _build_visual_annotation_regions(
    assignments: dict[int, _AnnotationRelation],
    blocks: list[dict[str, Any]],
    page_size: tuple[float, float],
) -> list[list[dict[str, Any]]]:
    """Expand each virtual small area in order of pre-title, main body, post-title, and footnote."""

    regions: list[list[dict[str, Any]]] = []
    for parent_indices, relations in _merge_parent_assignment_groups(assignments):
        leading = [
            index
            for index, relation in relations.items()
            if blocks[index].get("type") == "caption" and relation.direction in {"above", "left"}
        ]
        trailing = [
            index
            for index, relation in relations.items()
            if blocks[index].get("type") == "caption" and relation.direction in {"below", "right"}
        ]
        footnotes = [index for index in relations if blocks[index].get("type") == "footnote"]
        # The double-panel description follows its own table body to ensure that the public attribution stage is not mistakenly bound because the table body of another column is closer.
        ordered_bodies = []
        panel_notes = set()
        for body in _sort_visual_body_members(parent_indices, blocks, page_size):
            ordered_bodies.append(body)
            body_index = next(index for index in parent_indices if blocks[index] is body)
            if body.get("type") != "table":
                continue
            notes = [
                index
                for index in trailing
                if len(relations[index].parent.member_indices) == 1
                and relations[index].parent.member_indices[0] == body_index
                and re.match(r"^\s*\([a-z]\)\s+\S", str(blocks[index].get("content", "")))
            ]
            for index in _sort_annotation_indices_by_visual_rows(notes, blocks, page_size, relations):
                ordered_bodies.append(blocks[index])
                panel_notes.add(index)
        trailing = [index for index in trailing if index not in panel_notes]
        regions.append(
            [
                *(
                    blocks[index]
                    for index in _sort_annotation_indices_by_visual_rows(
                        leading,
                        blocks,
                        page_size,
                        relations,
                    )
                ),
                *ordered_bodies,
                *(
                    blocks[index]
                    for index in _sort_annotation_indices_by_visual_rows(
                        trailing,
                        blocks,
                        page_size,
                        relations,
                    )
                ),
                *(
                    blocks[index]
                    for index in sorted(
                        footnotes,
                        key=lambda value: _annotation_local_sort_key(
                            value,
                            blocks,
                            page_size,
                        ),
                    )
                ),
            ]
        )
    return regions


def _classify_and_bind_visual_annotations(
    blocks: list[dict[str, Any]],
    page_size: tuple[float, float],
    *,
    merge_text_block_group: _TextBlockGroupMerger | None = None,
) -> list[list[dict[str, Any]]]:
    """Reclassifies strong rule independent annotations and returns an ordered visual region for use by global XYCut++."""

    candidates = _collect_annotation_candidates(blocks)
    if not candidates:
        return []
    line_heights = [_block_median_line_height(blocks[index], page_size) for index in candidates]
    component_line_height = statistics.median(line_heights) if line_heights else 8.0
    parents = _build_visual_parents(blocks, page_size, component_line_height)
    if not parents:
        return []
    _add_caption_supported_table_panel_parents(blocks, page_size, parents, candidates)
    annotation_indices = set(candidates)
    assignments = {
        index: relation
        for index, kind in candidates.items()
        if (
            relation := _choose_annotation_relation(
                index,
                kind,
                blocks,
                page_size,
                parents,
                annotation_indices,
            )
        )
        is not None
    }
    for index in candidates:
        parent_block = blocks[index].get("_annotation_band_parent")
        if parent_block is None:
            continue
        parent = next(
            (
                parent
                for parent in parents
                if len(parent.member_indices) == 1 and blocks[parent.member_indices[0]] is parent_block
            ),
            None,
        )
        if parent is not None:
            previous = assignments.get(index)
            # Old legend bands established by decorative dividers cannot override the parent relationship already established by the data grid and graph semantics.
            if (
                candidates[index] == "caption"
                and parent.native_numeric_grid
                and previous is not None
                and not previous.parent.native_numeric_grid
                and re.search(r"\b(?:graph|chart|plot)\b", str(blocks[index].get("content", "")), re.I)
            ):
                continue
            direction = "above" if blocks[index]["bbox"][3] <= parent_block["bbox"][1] else "below"
            assignments[index] = _AnnotationRelation(parent, direction, 0.0, 1.0, 1.0, 0.0)
    assignments.update(
        _expand_stacked_bilingual_caption_assignments(
            blocks,
            page_size,
            candidates,
            assignments,
        )
    )
    cross_lane_assignments = _expand_cross_lane_caption_assignments(
        blocks,
        page_size,
        parents,
        candidates,
        assignments,
    )
    candidates.update(dict.fromkeys(cross_lane_assignments, "caption"))
    assignments.update(cross_lane_assignments)
    consumed_indices = _merge_table_footnote_continuations(
        blocks,
        candidates,
        assignments,
        page_size,
        merge_text_block_group,
    )
    for index, relation in assignments.items():
        blocks[index]["type"] = candidates[index]
        blocks[index]["_visual_annotation_direction"] = relation.direction
        # When native data grid evidence outperforms the nearest parent object, the internal association is retained for consumption by the public MiddleJson transformation and subsequently cleared.
        if (
            candidates[index] == "caption"
            and len(relation.parent.member_indices) == 1
            and not relation.parent.native_numeric_grid
            and any(parent.native_numeric_grid for parent in parents)
            and re.search(r"\b(?:graph|chart|plot)\b", str(blocks[index].get("content", "")), re.I)
        ):
            blocks[index]["_native_annotation_parent_bbox"] = blocks[relation.parent.member_indices[0]]["bbox"]
    regions = _build_visual_annotation_regions(assignments, blocks, page_size)
    if consumed_indices:
        blocks[:] = [block for index, block in enumerate(blocks) if index not in consumed_indices]
    return regions


def _expand_stacked_bilingual_caption_assignments(
    blocks: list[dict[str, Any]],
    page_size: tuple[float, float],
    candidates: dict[int, _AnnotationKind],
    assignments: dict[int, _AnnotationRelation],
) -> dict[int, _AnnotationRelation]:
    """Bind Chinese and English bilingual picture titles that are adjacent and numbered the same to the same visual subject."""

    output: dict[int, _AnnotationRelation] = {}
    assigned_captions = [index for index in assignments if candidates.get(index) == "caption"]
    for candidate_index, kind in candidates.items():
        if kind != "caption" or candidate_index in assignments:
            continue
        candidate_identifier = _caption_identifier(
            str(blocks[candidate_index].get("content") or ""),
        )
        candidate_bbox = _block_local_bbox(
            blocks[candidate_index],
            page_size,
        )
        if candidate_identifier is None or candidate_bbox is None:
            continue
        candidate_height = _block_median_line_height(
            blocks[candidate_index],
            page_size,
        )
        matches: list[tuple[float, int]] = []
        for anchor_index in assigned_captions:
            if _caption_identifier(
                str(blocks[anchor_index].get("content") or ""),
            ) != candidate_identifier or _block_angle(blocks[anchor_index]) != _block_angle(blocks[candidate_index]):
                continue
            anchor_bbox = _block_local_bbox(
                blocks[anchor_index],
                page_size,
            )
            if anchor_bbox is None:
                continue
            anchor_height = _block_median_line_height(
                blocks[anchor_index],
                page_size,
            )
            gap = max(
                candidate_bbox[1] - anchor_bbox[3],
                anchor_bbox[1] - candidate_bbox[3],
                0.0,
            )
            if (
                gap <= 1.5 * max(candidate_height, anchor_height)
                and _bbox_axis_overlap_ratio(
                    candidate_bbox,
                    anchor_bbox,
                    axis="x",
                )
                >= 0.35
            ):
                matches.append((gap, anchor_index))
        if not matches:
            continue
        _gap, anchor_index = min(matches)
        output[candidate_index] = assignments[anchor_index]
    return output
