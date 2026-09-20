"""覆盖短尾迁移的终止性、唯一归属及前序依赖；死循环反例在可回收子进程内运行。"""

from __future__ import annotations

import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from docvortex.analyzers.native.pdf.line_layout import _infer_text_lanes, _reattach_cross_lane_short_tails
from docvortex.analyzers.native.pdf.models import _LineItem, _TextLane
from docvortex.schema import BBox


def _line(text: str, bbox: BBox, index: int) -> tuple[_LineItem, BBox]:
    """构造具有明确有效行高的文本行及局部几何。"""
    return _LineItem(text=text, bbox=bbox, angle=0, source_index=index, effective_height=10.0), bbox


def _ambiguous_lanes(offset: float = 0.0, tails: int = 1) -> list[_TextLane]:
    """构造两栏均可接纳的短尾组，并用横向偏移隔离不同组。"""
    first = _TextLane(left=offset, right=offset + 200.0)
    second = _TextLane(left=offset, right=offset + 200.0)
    first.lines.append(_line("first predecessor", (offset + 10, 100, offset + 150, 110), 0))
    second.lines.append(_line("second predecessor", (offset + 10, 100, offset + 150, 110), 1))
    for index in range(tails):
        first.lines.append(_line(f"tail {index}", (offset + 15, 118, offset + 140, 128), index + 2))
    return [first, second]


def _membership(lanes: list[_TextLane]) -> tuple[tuple[int, ...], ...]:
    """按对象身份记录栏内顺序，避免重复文本或 source_index 掩盖丢行。"""
    return tuple(tuple(id(line) for line, _bbox in lane.lines) for lane in lanes)


def _run_and_check_stability(lanes: list[_TextLane]) -> None:
    """验证迁移不丢行、不重复、不改边界，且再次执行保持同一结果。"""
    before = Counter(id(line) for lane in lanes for line, _bbox in lane.lines)
    bounds = [(lane.left, lane.right, lane.is_span) for lane in lanes]
    _reattach_cross_lane_short_tails(lanes, 10.0)
    assert Counter(id(line) for lane in lanes for line, _bbox in lane.lines) == before
    assert [(lane.left, lane.right, lane.is_span) for lane in lanes] == bounds
    settled = _membership(lanes)
    _reattach_cross_lane_short_tails(lanes, 10.0)
    assert _membership(lanes) == settled


def _case_ambiguous_current_lane() -> None:
    """验证当前栏与其他栏同时匹配时保留原位，不发生往返振荡。"""
    lanes = _ambiguous_lanes()
    original = _membership(lanes)
    _run_and_check_stability(lanes)
    assert _membership(lanes) == original


def _case_independent_groups_and_valid_tail() -> None:
    """验证多个独立歧义组不会耗尽轮数或阻止稍后的合法短尾迁回。"""
    lanes = _ambiguous_lanes(tails=8) + _ambiguous_lanes(offset=400.0, tails=8)
    original = _membership(lanes)
    destination = _TextLane(left=810.0, right=1000.0)
    previous = _line("late predecessor", (815, 200, 965, 210), 0)
    tail = _line("late valid tail", (815, 218, 965, 228), 1)
    destination.lines.append(previous)
    lanes[0].lines.append(tail)
    lanes.append(destination)
    _run_and_check_stability(lanes)
    assert _membership(lanes[:-1]) == original
    assert destination.lines == [previous, tail]


def _case_inferred_body_and_span_lanes() -> None:
    """验证正常栏推断生成的正文栏和跨栏栏带不会互相争抢短尾。"""
    geometry = [_line("wide predecessor", (0, 100, 300, 110), 0)]
    for y in (103.0, 140.0, 160.0):
        for x in (0.0, 160.0):
            geometry.append(_line(f"column {x} {y}", (x, y, x + 140, y + 10), len(geometry)))
    tail = _line("tail", (0, 118, 100, 128), len(geometry))
    geometry.append(tail)
    lanes = _infer_text_lanes(geometry, 300.0, 10.0)
    assert len(lanes) == 3
    assert any(lane.is_span for lane in lanes)
    assert tail in lanes[0].lines
    _run_and_check_stability(lanes)


def _case_chained_tails() -> None:
    """验证逆序输入的连续短尾依次迁回，重复 source_index 不会漏掉后续行。"""
    previous = _line("predecessor", (10, 100, 150, 110), 0)
    first = _line("first tail", (10, 118, 150, 128), 1)
    second = _line("second tail", (10, 136, 130, 146), 1)
    source = _TextLane(left=210, right=400, lines=[second, first])
    target = _TextLane(left=0, right=200, lines=[previous])
    _run_and_check_stability([source, target])
    assert source.lines == []
    assert target.lines == [previous, first, second]


def _case_predecessor_changes_membership() -> None:
    """验证先迁移上方行后再判断短尾，不能用最初的栏状态一次性计算所有目标。"""
    anchor = _line("anchor", (0, 100, 200, 110), 0)
    first = _line("first tail", (0, 118, 140, 128), 1)
    second = _line("second tail", (0, 136, 100, 146), 2)
    source = _TextLane(left=0, right=300, lines=[second, first])
    target = _TextLane(left=0, right=200, lines=[anchor])
    _run_and_check_stability([source, target])
    assert source.lines == []
    assert target.lines == [anchor, first, second]


def _case_equal_height_peers() -> None:
    """验证同高度的先处理行不会成为另一个同行片段的前序证据。"""
    anchor = _line("anchor", (10, 100, 150, 110), 0)
    first = _line("first peer", (10, 118, 150, 128), 1)
    second = _line("second peer", (10, 118, 130, 128), 2)
    first[0].visual_row_id = 2
    second[0].visual_row_id = 2
    anchor[0].visual_row_id = 1
    source = _TextLane(left=210, right=400, lines=[second, first])
    target = _TextLane(left=0, right=200, lines=[anchor])
    _run_and_check_stability([source, target])
    assert source.lines == []
    assert target.lines == [anchor, first, second]


def _case_non_unique_or_missing_target() -> None:
    """验证两个其他栏匹配、没有匹配和非正文行均保持原位。"""
    targets = _ambiguous_lanes(tails=0)
    ambiguous = _line("two targets", (15, 118, 140, 128), 2)
    missing = _line("no target", (415, 118, 540, 128), 3)
    title = _line("title", (15, 118, 140, 128), 4)
    title[0].semantic_type = "paragraph_title"
    source = _TextLane(left=600, right=800, lines=[ambiguous, missing, title])
    lanes = [source, *targets]
    original = _membership(lanes)
    _run_and_check_stability(lanes)
    assert _membership(lanes) == original


def _case_same_position_predecessors() -> None:
    """验证同坐标前序行的平局选择不随迁入后排序改变，首次调用即得到稳定归属。"""
    first_anchor = _line("A anchor", (10, 100, 150, 110), 1)
    second_anchor = _line("B anchor", (10, 100, 150, 110), 2)
    first_tail = _line("A tail", (10, 118, 150, 128), 3)
    second_tail = _line("B tail", (10, 118.5, 150, 128.5), 4)
    for item, family in ((first_anchor, "A"), (second_anchor, "B"), (first_tail, "A"), (second_tail, "B")):
        item[0].font_signature = (family, 0)
        item[0].font_coverage = 1.0
    target = _TextLane(left=0, right=200, lines=[second_anchor, first_anchor])
    source = _TextLane(left=300, right=500, lines=[first_tail, second_tail])
    _run_and_check_stability([target, source])
    assert target.lines == [first_anchor, second_anchor, first_tail]
    assert source.lines == [second_tail]


@pytest.mark.parametrize(
    "case",
    [
        "ambiguous_current_lane",
        "independent_groups_and_valid_tail",
        "inferred_body_and_span_lanes",
        "chained_tails",
        "predecessor_changes_membership",
        "equal_height_peers",
        "non_unique_or_missing_target",
        "same_position_predecessors",
    ],
)
def test_short_tail_assignment_terminates_and_preserves_members(case: str) -> None:
    """在可强制终止并回收的子进程内检查归属，任何异常通过退出码和标准错误上报。"""
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), case],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


if __name__ == "__main__":
    globals()[f"_case_{sys.argv[1]}"]()
