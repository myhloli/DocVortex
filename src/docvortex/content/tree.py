"""严格文档树的公共遍历能力。"""

from collections.abc import Iterator

from ..schema import BlockBase, ImagePayloadBlock
from ..schema import _iter_child_blocks as iter_child_blocks


def iter_image_payloads(block: BlockBase) -> Iterator[ImagePayloadBlock]:
    """按当前节点优先的深度优先顺序遍历图片载荷，不修改文档树。"""
    if isinstance(block, ImagePayloadBlock):
        yield block
    for child in iter_child_blocks(block):
        yield from iter_image_payloads(child)


__all__ = ["iter_child_blocks", "iter_image_payloads"]
