"""Public traversal capabilities of strict document trees."""

from collections.abc import Iterator

from ..schema import BlockBase, ImagePayloadBlock
from ..schema import _iter_child_blocks as iter_child_blocks


def iter_image_payloads(block: BlockBase) -> Iterator[ImagePayloadBlock]:
    """Traverse the image payload in depth-first order based on the current node first, without modifying the document tree."""
    if isinstance(block, ImagePayloadBlock):
        yield block
    for child in iter_child_blocks(block):
        yield from iter_image_payloads(child)


__all__ = ["iter_child_blocks", "iter_image_payloads"]
