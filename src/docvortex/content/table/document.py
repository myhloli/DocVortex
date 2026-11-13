"""文档页边界上的跨页表格识别与延续标记编排。"""

from __future__ import annotations

from typing import Any, Callable

from ...schema import MERGE_TRANSPARENT_BLOCK_TYPES, BlockType

from .blocks import _get_or_create_table_state
from .models import BlockDict, PageInfoDict, TableMergeDecision, TableMergeRejection, TableMergeState
from .structure import _table_merge_rejection_reason

TABLE_BOUNDARY_IGNORED_TYPES = set(MERGE_TRANSPARENT_BLOCK_TYPES)


def _clear_table_continuation_marker(table_block: BlockDict) -> None:
    """递归清除 table 根块及其子块中过期的 ``continues_prev``。"""
    table_block.pop("continues_prev", None)
    content = table_block.get("content")
    if not isinstance(content, list):
        return
    for child in content:
        if isinstance(child, dict):
            child.pop("continues_prev", None)
            _clear_nested_continuation_markers(child)


def _clear_nested_continuation_markers(block: BlockDict) -> None:
    """清除表格子树中的旧延续标记，避免标记落到嵌套子块。"""
    content = block.get("content")
    if not isinstance(content, list):
        return
    for child in content:
        if isinstance(child, dict):
            child.pop("continues_prev", None)
            _clear_nested_continuation_markers(child)


def _find_boundary_table_with_blocker(
    blocks: list[Any],
    *,
    from_end: bool,
) -> tuple[BlockDict | None, str | None]:
    """返回边界表格，或首个阻断块的类型。"""
    ordered_blocks = reversed(blocks) if from_end else iter(blocks)
    for block in ordered_blocks:
        if not isinstance(block, dict):
            return None, type(block).__name__
        block_type = block.get("type")
        if block_type in TABLE_BOUNDARY_IGNORED_TYPES:
            continue
        if block_type == BlockType.TABLE:
            return block, None
        return None, block_type if isinstance(block_type, str) else type(block_type).__name__
    return None, None


def _find_boundary_table(blocks: list[Any], *, from_end: bool) -> BlockDict | None:
    """从页边界扫描 table；噪声块可跳过，其他语义块立即阻断。"""
    table_block, _ = _find_boundary_table_with_blocker(blocks, from_end=from_end)
    return table_block


def _is_consecutive_page_pair(previous_page: PageInfoDict, current_page: PageInfoDict) -> bool:
    """按显式零基 page_idx 判断页面在文档中是否严格连续。"""
    previous_page_idx = previous_page.get("page_idx")
    current_page_idx = current_page.get("page_idx")
    return type(previous_page_idx) is int and type(current_page_idx) is int and current_page_idx == previous_page_idx + 1


def _page_index(page: Any) -> int | None:
    """读取诊断使用的合法页索引，非法页不抛出额外异常。"""
    if not isinstance(page, dict):
        return None
    page_idx = page.get("page_idx")
    return page_idx if type(page_idx) is int else None


def _table_index(table_block: BlockDict | None) -> int | None:
    """读取诊断使用的表格块索引。"""
    if not isinstance(table_block, dict):
        return None
    index = table_block.get("index")
    return index if type(index) is int else None


def _report_decision(
    on_decision: Callable[[TableMergeDecision], None] | None,
    *,
    previous_page: Any,
    current_page: Any,
    rejection: TableMergeRejection | None,
    previous_table: BlockDict | None = None,
    current_table: BlockDict | None = None,
    blocker_page: Any = None,
    blocker_block_type: str | None = None,
) -> None:
    """仅在调用方请求时构造并发送不可变诊断。"""
    if on_decision is None:
        return
    on_decision(
        TableMergeDecision(
            previous_page_idx=_page_index(previous_page),
            current_page_idx=_page_index(current_page),
            previous_table_index=_table_index(previous_table),
            current_table_index=_table_index(current_table),
            rejection=rejection,
            blocker_page_idx=_page_index(blocker_page),
            blocker_block_type=blocker_block_type,
        )
    )


def merge_table(
    page_info_list: list[PageInfoDict],
    *,
    on_decision: Callable[[TableMergeDecision], None] | None = None,
) -> None:
    """倒序识别连续页边界表格，并只在后表写入延续标记。

    ``on_decision`` 可选接收不含正文的检测诊断，不改变默认标记行为。
    诊断只覆盖延续检测，不承诺后续渲染能够合并表格内容。
    """
    if on_decision is not None and not callable(on_decision):
        raise TypeError("on_decision must be callable or None")
    if not isinstance(page_info_list, list):
        return

    for page_info in page_info_list:
        if not isinstance(page_info, dict):
            continue
        blocks = page_info.get("blocks")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == BlockType.TABLE:
                _clear_table_continuation_marker(block)

    state_cache: dict[int, TableMergeState] = {}

    for page_position in range(len(page_info_list) - 1, 0, -1):
        current_page = page_info_list[page_position]
        previous_page = page_info_list[page_position - 1]
        if not isinstance(current_page, dict) or not isinstance(previous_page, dict):
            _report_decision(
                on_decision,
                previous_page=previous_page,
                current_page=current_page,
                rejection=TableMergeRejection.INVALID_PAGE,
            )
            continue
        if not _is_consecutive_page_pair(previous_page, current_page):
            _report_decision(
                on_decision,
                previous_page=previous_page,
                current_page=current_page,
                rejection=TableMergeRejection.NON_CONSECUTIVE_PAGES,
            )
            continue

        current_blocks = current_page.get("blocks")
        previous_blocks = previous_page.get("blocks")
        if not isinstance(current_blocks, list) or not isinstance(previous_blocks, list):
            _report_decision(
                on_decision,
                previous_page=previous_page,
                current_page=current_page,
                rejection=TableMergeRejection.INVALID_PAGE,
            )
            continue

        current_table_block, current_blocker_type = _find_boundary_table_with_blocker(current_blocks, from_end=False)
        previous_table_block, previous_blocker_type = _find_boundary_table_with_blocker(previous_blocks, from_end=True)
        if current_table_block is None or previous_table_block is None:
            current_is_blocked = current_table_block is None and current_blocker_type is not None
            previous_is_blocked = previous_table_block is None and previous_blocker_type is not None
            _report_decision(
                on_decision,
                previous_page=previous_page,
                current_page=current_page,
                rejection=(
                    TableMergeRejection.BOUNDARY_BLOCKED
                    if current_is_blocked or previous_is_blocked
                    else TableMergeRejection.BOUNDARY_TABLE_MISSING
                ),
                previous_table=previous_table_block,
                current_table=current_table_block,
                blocker_page=current_page if current_is_blocked else previous_page if previous_is_blocked else None,
                blocker_block_type=current_blocker_type if current_is_blocked else previous_blocker_type,
            )
            continue
        if "_pdf_structure" in current_table_block or "_pdf_structure" in previous_table_block:
            _report_decision(
                on_decision,
                previous_page=previous_page,
                current_page=current_page,
                rejection=TableMergeRejection.PDF_STRUCTURE_PRESERVED,
                previous_table=previous_table_block,
                current_table=current_table_block,
            )
            continue

        current_state = _get_or_create_table_state(current_table_block, state_cache)
        previous_state = _get_or_create_table_state(previous_table_block, state_cache)
        if current_state is None or previous_state is None:
            _report_decision(
                on_decision,
                previous_page=previous_page,
                current_page=current_page,
                rejection=TableMergeRejection.INVALID_TABLE_STRUCTURE,
                previous_table=previous_table_block,
                current_table=current_table_block,
            )
            continue

        rejection = _table_merge_rejection_reason(current_state, previous_state)
        if rejection is not None:
            _report_decision(
                on_decision,
                previous_page=previous_page,
                current_page=current_page,
                rejection=rejection,
                previous_table=previous_table_block,
                current_table=current_table_block,
            )
            continue

        current_table_block["continues_prev"] = True
        _report_decision(
            on_decision,
            previous_page=previous_page,
            current_page=current_page,
            rejection=None,
            previous_table=previous_table_block,
            current_table=current_table_block,
        )
