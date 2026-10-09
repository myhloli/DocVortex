"""各语义渲染器共用的可见书签目标遍历。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TypeAlias

from ....content.inline import inline_plain_text
from ....schema import (
    BlockBase,
    ChartAnnotationBlock,
    ChartBlock,
    CodeAnnotationBlock,
    CodeBlock,
    ImageAnnotationBlock,
    ImageBlock,
    MiddleJson,
    PageFootnoteBlock,
    TableAnnotationBlock,
    TableBlock,
    TextBlock,
    TitleBlockBase,
)

VisualAnnotation: TypeAlias = ImageAnnotationBlock | TableAnnotationBlock | ChartAnnotationBlock | CodeAnnotationBlock
AnchorBlock: TypeAlias = TextBlock | TitleBlockBase | PageFootnoteBlock | VisualAnnotation
VISUAL_ANNOTATION_TYPES = (ImageAnnotationBlock, TableAnnotationBlock, ChartAnnotationBlock, CodeAnnotationBlock)


def visible_block_anchor(block: AnchorBlock) -> str | None:
    """只给有可见行内内容的块返回原始书签，空块不占用同名说明目标。"""
    return block.anchor if inline_plain_text(block.content).strip() else None


def iter_visual_annotations(block: BlockBase) -> Iterator[VisualAnnotation]:
    """按源顺序枚举视觉父块的说明，不把主体或目录引用当成目标。"""
    if isinstance(block, (ImageBlock, TableBlock, ChartBlock, CodeBlock)):
        for child in block.content:
            if isinstance(child, VISUAL_ANNOTATION_TYPES):
                yield child


def iter_document_anchor_blocks(middle_json: MiddleJson) -> Iterator[AnchorBlock]:
    """按文档顺序枚举非空正文、标题、页面脚注及视觉说明的书签目标。"""
    for page in middle_json.pages:
        for block in page.blocks:
            candidates = (
                (block,)
                if isinstance(block, (TextBlock, TitleBlockBase, PageFootnoteBlock))
                else iter_visual_annotations(block)
            )
            for candidate in candidates:
                if (visible_block_anchor(candidate) or "").strip():
                    yield candidate
