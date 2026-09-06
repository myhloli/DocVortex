"""验证已规范化几何的阶段内复用，保留两类原始输入校验契约。"""

from __future__ import annotations

import json
import random

from docvortex.analyzers.native.pdf.geometry import _clip_bbox, _clip_validated_bbox, _coerce_bbox


def test_validated_clipping_matches_original_for_finite_boxes() -> None:
    """边界、反向输入与退化页尺寸产生完全相同的坐标值和 JSON 数值类型。"""
    random_source = random.Random(604)
    raw_boxes = [None, (0, 0, 0, 1), (5, 9, -3, -7), (float("nan"), 0, 1, 1)]
    raw_boxes.extend(tuple(random_source.uniform(-100, 100) for _ in range(4)) for _ in range(1000))
    for raw in raw_boxes:
        normalized = _coerce_bbox(raw)
        for size in ((600, 800), (40.5, 70.5), (0, 10), (-1, 10), (float("nan"), 10)):
            assert json.dumps(_clip_validated_bbox(normalized, size)) == json.dumps(_clip_bbox(normalized, size))


def test_raw_geometry_contracts_remain_distinct() -> None:
    """版面校验可整理反向框，行内证据必须继续拒绝反向框，不能混用快路径。"""
    from docvortex.analyzers.native.pdf.inline.common import _coerce_bbox as inline_bbox

    reversed_box = (10, 20, 0, 0)
    assert _coerce_bbox(reversed_box) == (0, 0, 10, 20)
    assert inline_bbox(reversed_box) is None
    assert _coerce_bbox((0, 0, float("inf"), 1)) is None
    assert inline_bbox((0, 0, float("inf"), 1)) is None
