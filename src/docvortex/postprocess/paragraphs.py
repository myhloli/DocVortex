"""PDF raw model-list Post-processing of paragraph continuation relationships."""

import math
from typing import Any, TypeAlias

from ..content.spans import inline_span_plain_text
from ..schema import MERGE_TRANSPARENT_BLOCK_TYPES, BlockType

LINE_STOP_FLAG = (".", "!", "?", "。", "！", "？", ")", "）", '"', "”", ":", "：", ";", "；")
SECTION_MERGE_BARRIER_TYPES = {
    BlockType.DOC_TITLE,
    BlockType.PARAGRAPH_TITLE,
    BlockType.EQUATION,
}
TEXT_MERGE_BARRIER_TYPES = {
    *SECTION_MERGE_BARRIER_TYPES,
    BlockType.LIST,
}
# Text paragraph merging is allowed across visual root blocks, page footers, and page decoration blocks, other semantic blocks will still block candidate lookups.
TEXT_MERGE_TRANSPARENT_TYPES = {
    BlockType.IMAGE,
    BlockType.TABLE,
    BlockType.CHART,
    BlockType.CODE,
    *MERGE_TRANSPARENT_BLOCK_TYPES,
}
CONTINUABLE_TEXT_BLOCK_TYPES = {BlockType.TEXT, BlockType.REF_TEXT}
VERTICAL_LINE_HEIGHT_TO_WIDTH_RATIO_THRESHOLD = 2
VERTICAL_LINE_IN_BLOCK_THRESHOLD = 0.8
SINGLE_LINE_LOOKAHEAD_LIMIT = 5
SINGLE_LINE_MIN_ALIGNED_LOOKAHEAD = 3
SINGLE_LINE_THICKNESS_RATIO_MAX = 1.5
BlockDict: TypeAlias = dict[str, Any]
CalculationBBox: TypeAlias = tuple[int, int, int, int]
OrderedBlock: TypeAlias = tuple[int, int, BlockDict]


def merge_para_text_blocks(pages: list[dict[str, Any]]) -> None:
    """Mark continues_prev for continuation of text or reference lists in page reading order."""
    ordered_blocks: list[OrderedBlock] = []
    for page_info in pages:
        blocks = page_info.get("blocks")
        if not isinstance(blocks, list):
            continue

        for block in blocks:
            if isinstance(block, dict):
                _clear_nested_continues_prev(block)
                if block.get("type") not in CONTINUABLE_TEXT_BLOCK_TYPES:
                    block.pop("continues_prev", None)

        page_idx = page_info.get("page_idx")
        if not isinstance(page_idx, int):
            continue
        for order_idx, block in enumerate(blocks):
            if isinstance(block, dict):
                ordered_blocks.append((page_idx, order_idx, block))

    for current_index in range(len(ordered_blocks) - 1, -1, -1):
        current_page_idx, _, current_block = ordered_blocks[current_index]
        current_type = current_block.get("type")
        if current_type in CONTINUABLE_TEXT_BLOCK_TYPES:
            # The result of lines that has been cleaned is considered complete for finalize, retaining its existing flags to support idempotent calls.
            if "lines" not in current_block:
                continue
            current_block.pop("continues_prev", None)
            if current_block.get("_reference_start") is True or current_block.get("_paragraph_boundary") is True:
                continue
            is_ref_text = current_type == BlockType.REF_TEXT
            previous_block = (
                _find_previous_ref_text_block(ordered_blocks, current_index)
                if is_ref_text
                else _find_previous_text_block(ordered_blocks, current_index)
            )
            if previous_block is None:
                continue
            previous_page_idx, _, previous_text_block = previous_block
            if not _is_same_or_consecutive_page(current_page_idx, previous_page_idx):
                continue
            if current_block.get("_reference_start") is False and previous_text_block.get("_reference_start") is not None:
                current_block["continues_prev"] = True
                continue
            can_merge = (
                can_auto_merge_ref_text_blocks(current_block, previous_text_block)
                if is_ref_text
                else can_auto_merge_text_blocks(current_block, previous_text_block)
            )
            if can_merge or _can_auto_merge_multiline_to_single_line(
                current_block,
                previous_text_block,
                ordered_blocks=ordered_blocks,
                current_index=current_index,
                current_page_idx=current_page_idx,
                current_type=current_type,
                require_current_leading_edge=not is_ref_text,
                reject_digit_or_uppercase_start=not is_ref_text,
            ):
                current_block["continues_prev"] = True
        elif current_type == BlockType.LIST:
            previous_block = _find_previous_ref_text_list_block(
                ordered_blocks,
                current_index,
                current_block,
            )
            if previous_block is None:
                continue
            previous_page_idx, _, _ = previous_block
            if _is_same_or_consecutive_page(current_page_idx, previous_page_idx):
                current_block["continues_prev"] = True

    for page_info in pages:
        blocks = page_info.get("blocks")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if isinstance(block, dict):
                _remove_line_metadata(block)


def can_auto_merge_text_blocks(current_block: BlockDict, previous_block: BlockDict) -> bool:
    """Determine whether the two dict text block are continuous according to the beginning and end of the text, line direction and geometric relationship."""
    return _can_auto_merge_continuable_text_blocks(
        current_block,
        previous_block,
        require_current_leading_edge=True,
        reject_digit_or_uppercase_start=True,
    )


def can_auto_merge_ref_text_blocks(current_block: BlockDict, previous_block: BlockDict) -> bool:
    """Determine ref_text according to the text rules, relax the current starting boundary and the restrictions on the beginning of numbers or uppercase characters."""
    return _can_auto_merge_continuable_text_blocks(
        current_block,
        previous_block,
        require_current_leading_edge=False,
        reject_digit_or_uppercase_start=False,
    )


def _can_auto_merge_continuable_text_blocks(
    current_block: BlockDict,
    previous_block: BlockDict,
    *,
    require_current_leading_edge: bool,
    reject_digit_or_uppercase_start: bool,
) -> bool:
    """Reuse common text boundaries, directions, and geometric continuation rules for the main text and references."""
    current_metric_lines = _metric_line_bboxes(current_block)
    previous_metric_lines = _metric_line_bboxes(previous_block)
    if not current_metric_lines or not previous_metric_lines:
        return False

    current_bbox = _bbox_for_calculation(current_block.get("bbox"))
    previous_bbox = _bbox_for_calculation(previous_block.get("bbox"))
    if current_bbox is None or previous_bbox is None:
        return False

    current_content = _normalized_text_content(current_block)
    previous_content = _normalized_text_content(previous_block)
    if not current_content or not previous_content:
        return False
    if not _has_mergeable_text_boundary(
        current_content,
        previous_content,
        reject_digit_or_uppercase_start=reject_digit_or_uppercase_start,
    ):
        return False

    current_is_vertical = _is_vertical_text_block_by_lines(current_metric_lines)
    previous_is_vertical = _is_vertical_text_block_by_lines(previous_metric_lines)
    if current_is_vertical != previous_is_vertical:
        return False
    if current_is_vertical:
        return _can_auto_merge_vertical_text_blocks(
            current_content,
            previous_content,
            current_bbox,
            previous_bbox,
            current_metric_lines,
            previous_metric_lines,
            require_current_leading_edge=require_current_leading_edge,
        )
    return _can_auto_merge_horizontal_text_blocks(
        current_content,
        previous_content,
        current_bbox,
        previous_bbox,
        current_metric_lines,
        previous_metric_lines,
        require_current_leading_edge=require_current_leading_edge,
    )


def _find_previous_text_block(
    ordered_blocks: list[OrderedBlock],
    current_index: int,
) -> OrderedBlock | None:
    """Looking forward to text, the visual root block and the merged transparent block can be crossed, and other semantic blocks will block the search."""
    for previous_index in range(current_index - 1, -1, -1):
        previous_block = ordered_blocks[previous_index][2]
        previous_type = previous_block.get("type")
        if previous_type in TEXT_MERGE_BARRIER_TYPES:
            return None
        if previous_type != BlockType.TEXT:
            if previous_type not in TEXT_MERGE_TRANSPARENT_TYPES:
                return None
            continue
        return ordered_blocks[previous_index]
    return None


def _find_previous_ref_text_block(
    ordered_blocks: list[OrderedBlock],
    current_index: int,
) -> OrderedBlock | None:
    """Skip page footers and auxiliary blocks to find the previous ref_text, other semantic blocks remain blocked."""
    for previous_index in range(current_index - 1, -1, -1):
        previous_block = ordered_blocks[previous_index][2]
        previous_type = previous_block.get("type")
        if previous_type in MERGE_TRANSPARENT_BLOCK_TYPES:
            continue
        if previous_type == BlockType.REF_TEXT:
            return ordered_blocks[previous_index]
        return None
    return None


def _find_previous_ref_text_list_block(
    ordered_blocks: list[OrderedBlock],
    current_index: int,
    current_block: BlockDict,
) -> OrderedBlock | None:
    """Skip page footers and auxiliary blocks to find the previous ref_text list, other semantic blocks remain blocked."""
    if not _is_ref_text_list_block(current_block):
        return None
    for previous_index in range(current_index - 1, -1, -1):
        previous_block = ordered_blocks[previous_index][2]
        if previous_block.get("type") in MERGE_TRANSPARENT_BLOCK_TYPES:
            continue
        if _is_ref_text_list_block(previous_block):
            return ordered_blocks[previous_index]
        return None
    return None


def _is_ref_text_list_block(block: BlockDict) -> bool:
    """Determine whether the current dict block is a reference list."""
    return block.get("type") == BlockType.LIST and block.get("sub_type") == BlockType.REF_TEXT


def _is_same_or_consecutive_page(current_page_idx: int, previous_page_idx: int) -> bool:
    """Only the same page or the following pages with strictly consecutive page numbers are allowed to establish a continuation relationship."""
    return current_page_idx == previous_page_idx or current_page_idx == previous_page_idx + 1


def _positive_values_have_max_ratio(first: float, second: float, max_ratio: float) -> bool:
    """Determine whether the larger and smaller ratio of two positive values does not exceed the given upper limit."""
    if first <= 0 or second <= 0:
        return False
    return max(first, second) / min(first, second) <= max_ratio


def _bbox_for_calculation(bbox: Any) -> CalculationBBox | None:
    """Copy and enlarge 0~1 bbox to thousandth integer, the original bbox remains unchanged."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        values = tuple(float(value) for value in bbox)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) and 0 <= value <= 1 for value in values):
        return None

    x0, y0, x1, y1 = (int(round(value * 1000)) for value in values)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def _metric_line_bboxes(block: BlockDict) -> list[CalculationBBox]:
    """Read all legal line boxes of block.lines. If any line is illegal, the whole block will be treated as unmergeable."""
    lines = block.get("lines")
    if not isinstance(lines, list) or not lines:
        return []

    line_bboxes: list[CalculationBBox] = []
    for line in lines:
        if not isinstance(line, dict):
            return []
        line_bbox = _bbox_for_calculation(line.get("bbox"))
        if line_bbox is None:
            return []
        line_bboxes.append(line_bbox)
    return line_bboxes


def _normalized_text_content(block: BlockDict) -> str:
    """Read the structured Span visible text of text block."""

    content = block.get("content")
    if not isinstance(content, list):
        return ""
    return inline_span_plain_text(item for item in content if isinstance(item, dict)).strip()


def _bbox_union(line_bboxes: list[CalculationBBox]) -> CalculationBBox:
    """Aggregate all line boxes to obtain text coverage that is only used for geometric judgments."""
    return (
        min(bbox[0] for bbox in line_bboxes),
        min(bbox[1] for bbox in line_bboxes),
        max(bbox[2] for bbox in line_bboxes),
        max(bbox[3] for bbox in line_bboxes),
    )


def _line_height(line_bbox: CalculationBBox) -> int:
    """Calculates the row box height in thousandths."""
    return line_bbox[3] - line_bbox[1]


def _line_width(line_bbox: CalculationBBox) -> int:
    """Calculates line box width in thousandths."""
    return line_bbox[2] - line_bbox[0]


def _is_vertical_text_block_by_lines(line_bboxes: list[CalculationBBox]) -> bool:
    """Determine whether block is vertical text based on the line frame aspect ratio."""
    vertical_line_count = sum(
        _line_height(line_bbox) / _line_width(line_bbox) > VERTICAL_LINE_HEIGHT_TO_WIDTH_RATIO_THRESHOLD
        for line_bbox in line_bboxes
    )
    return vertical_line_count / len(line_bboxes) > VERTICAL_LINE_IN_BLOCK_THRESHOLD


def _has_mergeable_text_boundary(
    current_content: str,
    previous_content: str,
    *,
    reject_digit_or_uppercase_start: bool,
) -> bool:
    """Use pre-block end and post-block start characters to exclude obvious new paragraph boundaries."""
    if previous_content.endswith(LINE_STOP_FLAG):
        return False
    if not reject_digit_or_uppercase_start:
        return True
    first_char = current_content[0]
    return not first_char.isdigit() and not first_char.isupper()


def _collect_following_same_orientation_lines(
    ordered_blocks: list[OrderedBlock],
    current_index: int,
    *,
    current_page_idx: int,
    current_type: str,
    is_vertical: bool,
) -> list[CalculationBBox]:
    """Read up to five lines of the same type and direction backwards on the current page. Semantic barriers or illegal text will terminate the reading."""
    following_lines: list[CalculationBBox] = []
    transparent_types = TEXT_MERGE_TRANSPARENT_TYPES if current_type == BlockType.TEXT else MERGE_TRANSPARENT_BLOCK_TYPES
    for page_idx, _, block in ordered_blocks[current_index + 1 :]:
        if page_idx != current_page_idx:
            break
        block_type = block.get("type")
        if block_type != current_type:
            if block_type in transparent_types:
                continue
            break

        block_lines = _metric_line_bboxes(block)
        if not block_lines or _is_vertical_text_block_by_lines(block_lines) != is_vertical:
            break
        for line_bbox in block_lines:
            following_lines.append(line_bbox)
            if len(following_lines) >= SINGLE_LINE_LOOKAHEAD_LIMIT:
                return following_lines
    return following_lines


def _aligned_following_lines(
    current_line: CalculationBBox,
    following_lines: list[CalculationBBox],
    *,
    is_vertical: bool,
) -> list[CalculationBBox]:
    """Filter subsequent rows and columns in the same virtual column according to the horizontal left boundary or the vertical upper boundary."""
    current_start = current_line[1] if is_vertical else current_line[0]
    current_thickness = _line_width(current_line) if is_vertical else _line_height(current_line)
    aligned_lines: list[CalculationBBox] = []
    for line_bbox in following_lines:
        line_start = line_bbox[1] if is_vertical else line_bbox[0]
        line_thickness = _line_width(line_bbox) if is_vertical else _line_height(line_bbox)
        if not _positive_values_have_max_ratio(
            current_thickness,
            line_thickness,
            SINGLE_LINE_THICKNESS_RATIO_MAX,
        ):
            continue
        if abs(line_start - current_start) <= max(current_thickness, line_thickness):
            aligned_lines.append(line_bbox)
    return aligned_lines


def _virtual_single_line_bbox(
    current_line: CalculationBBox,
    aligned_lines: list[CalculationBBox],
    *,
    is_vertical: bool,
) -> CalculationBBox:
    """Only the single-line calculation box is expanded along the main text axis, leaving the original line and block bbox unchanged."""
    if is_vertical:
        return (
            current_line[0],
            current_line[1],
            current_line[2],
            max(current_line[3], *(line_bbox[3] for line_bbox in aligned_lines)),
        )
    return (
        current_line[0],
        current_line[1],
        max(current_line[2], *(line_bbox[2] for line_bbox in aligned_lines)),
        current_line[3],
    )


def _can_auto_merge_multiline_to_single_line(
    current_block: BlockDict,
    previous_block: BlockDict,
    *,
    ordered_blocks: list[OrderedBlock],
    current_index: int,
    current_page_idx: int,
    current_type: str,
    require_current_leading_edge: bool,
    reject_digit_or_uppercase_start: bool,
) -> bool:
    """Use the subsequent five rows or columns to make up the main axis size of a single row, and then reuse the original horizontal or vertical connection rules."""
    current_lines = _metric_line_bboxes(current_block)
    previous_lines = _metric_line_bboxes(previous_block)
    if len(current_lines) != 1 or len(previous_lines) <= 1:
        return False

    current_is_vertical = _is_vertical_text_block_by_lines(current_lines)
    if _is_vertical_text_block_by_lines(previous_lines) != current_is_vertical:
        return False
    following_lines = _collect_following_same_orientation_lines(
        ordered_blocks,
        current_index,
        current_page_idx=current_page_idx,
        current_type=current_type,
        is_vertical=current_is_vertical,
    )
    aligned_lines = _aligned_following_lines(
        current_lines[0],
        following_lines,
        is_vertical=current_is_vertical,
    )
    if len(aligned_lines) < SINGLE_LINE_MIN_ALIGNED_LOOKAHEAD:
        return False

    current_bbox = _bbox_for_calculation(current_block.get("bbox"))
    previous_bbox = _bbox_for_calculation(previous_block.get("bbox"))
    current_content = _normalized_text_content(current_block)
    previous_content = _normalized_text_content(previous_block)
    if current_bbox is None or previous_bbox is None or not current_content or not previous_content:
        return False
    if not _has_mergeable_text_boundary(
        current_content,
        previous_content,
        reject_digit_or_uppercase_start=reject_digit_or_uppercase_start,
    ):
        return False

    virtual_current_line = _virtual_single_line_bbox(
        current_lines[0],
        aligned_lines,
        is_vertical=current_is_vertical,
    )
    if current_is_vertical:
        return _can_auto_merge_vertical_text_blocks(
            current_content,
            previous_content,
            current_bbox,
            previous_bbox,
            [virtual_current_line],
            previous_lines,
            require_current_leading_edge=require_current_leading_edge,
        )
    return _can_auto_merge_horizontal_text_blocks(
        current_content,
        previous_content,
        current_bbox,
        previous_bbox,
        [virtual_current_line],
        previous_lines,
        require_current_leading_edge=require_current_leading_edge,
    )


def _can_auto_merge_horizontal_text_blocks(
    current_content: str,
    previous_content: str,
    current_bbox: CalculationBBox,
    previous_bbox: CalculationBBox,
    current_lines: list[CalculationBBox],
    previous_lines: list[CalculationBBox],
    *,
    require_current_leading_edge: bool,
) -> bool:
    """Use the first line, last line, width of the horizontal paragraph and the block intersection rule to determine whether it is continuous."""
    first_line = current_lines[0]
    last_line = previous_lines[-1]
    first_line_height = _line_height(first_line)
    last_line_height = _line_height(last_line)
    if first_line_height <= 0 or last_line_height <= 0:
        return False

    current_lines_bbox = _bbox_union(current_lines)
    previous_lines_bbox = _bbox_union(previous_lines)
    if require_current_leading_edge and abs(current_lines_bbox[0] - first_line[0]) >= first_line_height / 2:
        return False
    if abs(previous_lines_bbox[2] - last_line[2]) >= last_line_height:
        return False
    current_width = current_lines_bbox[2] - current_lines_bbox[0]
    previous_width = previous_lines_bbox[2] - previous_lines_bbox[0]
    min_width = min(current_width, previous_width)
    if min_width <= 0 or abs(current_width - previous_width) >= min_width:
        return False
    if len(current_lines) <= 1 and len(previous_lines) <= 1:
        return False
    return current_bbox[1] < previous_bbox[3]


def _can_auto_merge_vertical_text_blocks(
    current_content: str,
    previous_content: str,
    current_bbox: CalculationBBox,
    previous_bbox: CalculationBBox,
    current_lines: list[CalculationBBox],
    previous_lines: list[CalculationBBox],
    *,
    require_current_leading_edge: bool,
) -> bool:
    """Use the first column, last column, height of the vertical paragraph and the block intersection rule to determine whether it is continuous."""
    first_line = current_lines[0]
    last_line = previous_lines[-1]
    first_line_width = _line_width(first_line)
    last_line_width = _line_width(last_line)
    if first_line_width <= 0 or last_line_width <= 0:
        return False

    current_lines_bbox = _bbox_union(current_lines)
    previous_lines_bbox = _bbox_union(previous_lines)
    if require_current_leading_edge and abs(current_lines_bbox[1] - first_line[1]) >= first_line_width / 2:
        return False
    if abs(previous_lines_bbox[3] - last_line[3]) >= last_line_width:
        return False
    current_height = current_lines_bbox[3] - current_lines_bbox[1]
    previous_height = previous_lines_bbox[3] - previous_lines_bbox[1]
    min_height = min(current_height, previous_height)
    if min_height <= 0 or abs(current_height - previous_height) >= min_height:
        return False
    return current_bbox[2] > previous_bbox[0]


def _clear_nested_continues_prev(block: BlockDict) -> None:
    """Recursively clean up old sub-block marks, and the top-level text/ref_text mark will be recalculated depending on whether there is still lines."""
    content = block.get("content")
    if not isinstance(content, list):
        return
    for child_block in content:
        if isinstance(child_block, dict):
            child_block.pop("continues_prev", None)
            _clear_nested_continues_prev(child_block)


def _remove_line_metadata(block: BlockDict) -> None:
    """Recursively delete temporary lines fields for top-level and nested blocks."""
    block.pop("lines", None)
    content = block.get("content")
    if not isinstance(content, list):
        return
    for child_block in content:
        if isinstance(child_block, dict):
            _remove_line_metadata(child_block)
