"""Native ModelJson Shared constant used by visual material processing."""

from ...schema import BlockType

IMAGE_BLOCK_CONTAINMENT_THRESHOLD = 0.8
MODEL_JSON_VISUAL_BLOCK_TYPES = {BlockType.IMAGE, BlockType.CHART, BlockType.TABLE, BlockType.EQUATION}
