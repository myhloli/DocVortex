"""各格式共用的逻辑块复制、延续合并与页面规划。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .context import claim_owned_document

from ....content.table import merge_table_content
from ...contracts import RenderMode
from ....schema import (
    MERGE_TRANSPARENT_BLOCK_TYPES,
    BlockType,
    ContinuableTextBlockBase,
    ListBlock,
    MiddleJson,
    PageBlock,
    InlineSpan,
    RefTextBlock,
    TableBlock,
    TextBlock,
)


@dataclass(slots=True)
class PlannedBlock:
    """保存一个待渲染块及其来源页和文本延续片段。"""

    page_idx: int
    block: PageBlock
    text_contents: list[list[InlineSpan]] = field(default_factory=list)
    removed: bool = False


def build_render_plan(
    middle_json: MiddleJson,
    mode: RenderMode = RenderMode.DEFAULT,
) -> list[list[PlannedBlock]]:
    """深拷贝 MiddleJson，并按模式生成不污染输入的逐页逻辑块计划。"""
    owned = claim_owned_document(middle_json)
    copied = middle_json if owned else middle_json.model_copy(deep=True)
    pages = [
        [
            PlannedBlock(
                page_idx=page.page_idx,
                block=block,
                text_contents=[block.content] if isinstance(block, ContinuableTextBlockBase) else [],
            )
            for block in page.blocks
        ]
        for page in copied.pages
    ]
    flattened = [planned for page in pages for planned in page]
    _merge_continued_text_blocks(flattened, mode)
    _merge_continued_list_blocks(flattened, mode, copy_on_merge=owned)
    if mode is RenderMode.DEFAULT:
        _merge_continued_table_blocks(flattened)
    return pages


def _merge_continued_text_blocks(blocks: list[PlannedBlock], mode: RenderMode) -> None:
    """把无独立正文锚点的 continues_prev 文本吸收到最近的前序文本逻辑块。"""
    previous_text: PlannedBlock | None = None
    previous_reference: PlannedBlock | None = None
    for current in blocks:
        is_text = isinstance(current.block, TextBlock)
        is_reference = isinstance(current.block, RefTextBlock)
        previous = previous_text if is_text else previous_reference if is_reference else None
        anchored = is_text and isinstance(current.block.anchor, str) and bool(current.block.anchor.strip())
        if (
            not current.removed
            and isinstance(current.block, ContinuableTextBlockBase)
            and current.block.continues_prev is True
            and not anchored
            and previous is not None
            and not (mode is RenderMode.FULL and previous.page_idx != current.page_idx)
        ):
            previous.text_contents.extend(current.text_contents)
            current.removed = True
        if is_text and not current.removed:
            previous_text = current
        if is_reference:
            if not current.removed:
                previous_reference = current
        elif current.block.type not in MERGE_TRANSPARENT_BLOCK_TYPES:
            previous_reference = None


def _merge_continued_list_blocks(blocks: list[PlannedBlock], mode: RenderMode, *, copy_on_merge: bool = False) -> None:
    """把续接列表吸收到子类型一致的前序列表，参考文献可跨过合并透明块。"""
    previous_list: PlannedBlock | None = None
    previous_reference_list: PlannedBlock | None = None
    copied: set[int] = set()
    for current in blocks:
        if not isinstance(current.block, ListBlock):
            previous_list = None
            if current.block.type not in MERGE_TRANSPARENT_BLOCK_TYPES:
                previous_reference_list = None
            continue
        previous = previous_reference_list if current.block.sub_type == BlockType.REF_TEXT else previous_list
        if (
            not current.removed
            and current.block.continues_prev is True
            and previous is not None
            and previous.block.sub_type == current.block.sub_type
            and not (mode is RenderMode.FULL and previous.page_idx != current.page_idx)
        ):
            # EPUB 等渲染器仍读取原文档；只有实际修改的列表需要另建副本。
            if copy_on_merge and id(previous) not in copied:
                previous.block = previous.block.model_copy(deep=True)
                copied.add(id(previous))
            previous.block.content.extend(current.block.content)
            current.removed = True
        if not current.removed:
            previous_list = previous_reference_list = current


def _merge_continued_table_blocks(blocks: list[PlannedBlock]) -> None:
    """在默认模式中把跨页续表合并到最近的前序表格。"""
    previous: PlannedBlock | None = None
    for current in blocks:
        if current.removed or not isinstance(current.block, TableBlock):
            continue
        if current.block.continues_prev is True and previous is not None and previous.page_idx != current.page_idx:
            merged = merge_table_content(
                previous.block.model_dump(mode="python", exclude_none=True),
                current.block.model_dump(mode="python", exclude_none=True),
            )
            if merged is not None:
                try:
                    previous.block = TableBlock.model_validate(merged)
                except (TypeError, ValueError):
                    pass
                else:
                    current.removed = True
        if not current.removed:
            previous = current


__all__ = ["PlannedBlock", "build_render_plan"]
