"""第七轮阶段缓存、普通字符源几何与物化复用的独立差分。"""

from copy import deepcopy
import math
import random

import pytest

from docvortex.analyzers.native.pdf.models import _LineItem, _TextLane
from docvortex.analyzers.native.pdf.title_analysis import body_profile, page_titles


def _profile_lane(seed):
    """构造排序、字号、字体平局及混合语义均可变化的栏成员。"""
    rng = random.Random(seed)
    rows = []
    for index in range(40):
        left = rng.choice([0.0, 30.0, 60.0])
        box = (left, float(index * 15), left + rng.choice([40.0, 80.0, 160.0]), float(index * 15 + 10))
        line = _LineItem("Section" if index % 3 == 0 else "Body text.", box, 0, index)
        line.effective_height = rng.choice([8.0, 10.0, 12.0])
        line.font_signature = (rng.choice(["A", "B"]), rng.choice([0, 64]))
        line.font_coverage = 1.0
        line.dominant_font_weight = rng.choice([400.0, 700.0])
        rows.append((line, box))
    rng.shuffle(rows)
    return _TextLane(0.0, 220.0, rows)


def test_profile_cache_preserves_first_sort_and_invalidates_semantics(monkeypatch):
    """第一次排序前后的字体首命中不可混用，语义变化须重算。"""
    lane = _profile_lane(7)
    expected_lane = deepcopy(lane)
    original = body_profile._infer_lane_body_profile
    calls = []

    def counted(value):
        """记录真实参考统计调用，检查缓存不会重复扫描稳定栏。"""
        calls.append(1)
        return original(value)

    monkeypatch.setattr(body_profile, "_infer_lane_body_profile", counted)
    context = body_profile._LaneProfileContext([lane])
    assert context.profile(lane) == original(expected_lane)
    assert [row[0].source_index for row in lane.lines] == [row[0].source_index for row in expected_lane.lines]
    assert context.profile(lane) == original(expected_lane)
    assert context.profile(lane) == original(expected_lane)
    assert len(calls) == 2
    lane.lines[0][0].semantic_type = "paragraph_title"
    expected_lane.lines[0][0].semantic_type = "paragraph_title"
    context.invalidate(lane.lines[0][0])
    assert context.profile(lane) == original(expected_lane)
    assert len(calls) == 3


@pytest.mark.parametrize("change", ["geometry", "font", "order", "members", "boundary"])
def test_profile_new_stage_rebuilds_mutated_state(change):
    """新分析阶段不沿用旧几何、字体、成员、栏界或顺序的缓存。"""
    lane = _profile_lane(3)
    old = body_profile._LaneProfileContext([lane])
    old.profile(lane)
    if change == "geometry":
        lane.lines[0][0].effective_height = 30.0
    elif change == "font":
        lane.lines[0][0].font_signature = ("C", 0)
    elif change == "order":
        lane.lines.reverse()
    elif change == "members":
        lane.lines.pop()
    else:
        lane.right = 400.0
    expected = deepcopy(lane)
    context = body_profile._LaneProfileContext([lane])
    assert context.profile(lane) == body_profile._infer_lane_body_profile(expected)
    assert [row[0].source_index for row in lane.lines] == [row[0].source_index for row in expected.lines]


@pytest.mark.parametrize("invalid", [math.nan, math.inf, 10**400])
def test_profile_abnormal_numeric_uses_reference(invalid):
    """异常尺度不能因缓存预检查引入额外异常或改变参考路径。"""
    lane = _profile_lane(2)
    lane.lines[0][0].effective_height = invalid
    assert not body_profile._LaneProfileContext([lane]).plain


@pytest.mark.parametrize("seed", range(20))
@pytest.mark.parametrize("operation", ["centered", "emphasized", "demote"])
def test_title_decisions_match_uncached_stage(seed, operation, monkeypatch):
    """原候选遍历与语义写入连续发生时，缓存仍与逐次统计一致。"""
    lane = _profile_lane(seed)
    lane.lines.sort(key=lambda row: row[0].source_index)
    if operation == "demote":
        for line, _box in lane.lines[::3]:
            line.semantic_type = "paragraph_title"
    expected = deepcopy(lane)
    function = {
        "centered": page_titles._classify_cross_lane_centered_section_titles,
        "emphasized": page_titles._classify_cross_lane_emphasized_section_titles,
        "demote": page_titles._demote_cross_lane_body_continuation_titles,
    }[operation]
    args = [] if operation == "demote" else [220.0, 650.0, []]
    kwargs = {} if operation == "demote" else {"document_title_bottom": None}
    if operation == "centered":
        kwargs["page_index"] = 1
    elif operation == "emphasized":
        kwargs["document_body_profile"] = None
    function(list(lane.lines), [lane], *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(
            body_profile._LaneProfileContext, "profile", lambda self, value: body_profile._infer_lane_body_profile(value)
        )
        function(list(expected.lines), [expected], *args, **kwargs)
    assert lane == expected
