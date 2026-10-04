"""用变形正例及缺证据反例限制原生表格、面板和成员排序规则。"""

from dataclasses import replace
import pytest
from docvortex.analyzers.native.pdf._table_recovery.banded_numeric import build_banded_numeric_candidate
from docvortex.analyzers.native.pdf._table_recovery.contracts import NativeTableInput, NativeTableRule
from docvortex.analyzers.native.pdf._table_recovery.text import build_native_table_text
from docvortex.analyzers.native.pdf.models import _LineItem
from docvortex.analyzers.native.pdf.title_analysis.panels import classify_panel_titles
from docvortex.analyzers.native.pdf.panel_order import numbered_step_member_groups
from test_native_pdf_table import _char_items
from docvortex.analyzers.native.pdf.graphics import _image_members_to_content


def _numeric_table(scale: float, offset: float) -> NativeTableInput:
    """构造两列三数据行的少线表，平移和缩放不应改变结构判断。"""
    entries = [("Material", (4, 5, 45, 13)), ("Value", (80, 5, 105, 13))]
    for row, value in enumerate(["10", "20", "30"]):
        y = 23 + row * 18
        entries.extend([(f"Mineral {row}", (4, y, 44, y + 8)), (value, (80, y, 92, y + 8))])
    entries = [(text, tuple(offset + v * scale for v in box)) for text, box in entries]
    end = offset + 120 * scale
    bottom = offset + 80 * scale
    return NativeTableInput(
        (offset, offset, end, bottom),
        (end + 10, bottom + 10),
        0,
        _char_items(entries),
        (
            NativeTableRule((offset, offset, end, offset + scale), scale, "horizontal"),
            NativeTableRule((offset, bottom - scale, end, bottom), scale, "horizontal"),
        ),
    )


@pytest.mark.parametrize("scale,offset", [(1, 0), (0.6, 19), (1.8, 31)])
def test_sparse_numeric_table_is_scale_and_position_independent(scale, offset):
    """重复数值列和物理外线共同证明四行两列，不依赖固定坐标。"""
    table = _numeric_table(scale, offset)
    text = build_native_table_text(table)
    candidate = build_banded_numeric_candidate(table, text, {})
    assert candidate is not None
    assert (candidate.rows, candidate.cols) == (4, 2)
    assert [cell.content for cell in candidate.cells if cell.col == 1] == ["Value", "10", "20", "30"]


def test_sparse_numeric_table_requires_physical_support():
    """没有底线或底色支持的段落不能仅凭数字列被恢复为表格。"""
    table = replace(_numeric_table(1, 0), drawing_lines=())
    assert build_banded_numeric_candidate(table, build_native_table_text(table), {}) is None


def test_sparse_numeric_table_rejects_crossing_glyph():
    """正文中跨列界的完整大字形不能被新候选拆成两个格。"""
    table = _numeric_table(1, 0)
    chars = (*table.chars, {"char": "W", "bbox": (40, 23, 84, 31), "char_idx": len(table.chars)})
    table = replace(table, chars=chars)
    assert build_banded_numeric_candidate(table, build_native_table_text(table), {}) is None


def _panel_lines(body_height: float = 10) -> list[_LineItem]:
    """构造等式标题与不同字号简介的重复两栏，不给出具体页面名称。"""
    lines = []
    for column in range(2):
        x = 20 + 220 * column
        for text, y, height in [("Panel subject", 20, 14), ("Several words describe the subject", 52, body_height)]:
            lines.append(
                _LineItem(
                    text,
                    (x, y, x + 140, y + height),
                    0,
                    len(lines),
                    effective_height=height,
                    font_signature=("Arial", 0),
                    font_coverage=1,
                )
            )
    return lines


def test_parallel_panel_titles_require_typographic_transition():
    """相邻完整面板的标题和小字号简介构成正证据。"""
    lines = _panel_lines()
    classify_panel_titles(lines, [], 500)
    assert [line.semantic_type for line in lines] == ["paragraph_title", None, "paragraph_title", None]


@pytest.mark.parametrize("kind", ["same_body_scale", "single_panel", "different_style"])
def test_panel_rule_rejects_unverified_parallel_prose(kind):
    """同字号正文、孤立标签或不同式行均不能借重复列布局提升为标题。"""
    lines = _panel_lines(14 if kind == "same_body_scale" else 10)
    if kind == "single_panel":
        lines = lines[:2]
    if kind == "different_style":
        lines[2].font_signature = ("Other", 0)
    classify_panel_titles(lines, [], 500)
    assert all(line.semantic_type is None for line in lines)


def test_step_member_group_requires_consecutive_children():
    """缺少中间子项时不宣称稳定字母序列，也不改变全局阅读顺序。"""
    blocks = [
        {"type": "text", "content": value, "bbox": (left, top, left + 130, top + 10)}
        for value, left, top in [
            ("1. Parent", 10, 10),
            ("a. Child", 25, 20),
            ("c. Child", 25, 30),
            ("d. Child", 25, 40),
            ("2. Next", 10, 50),
        ]
    ]
    assert numbered_step_member_groups(blocks, set()) == []


def test_rotated_axis_keeps_left_to_right_order_for_unequal_label_lengths():
    """共用轴线的长短标签按横坐标排列，不能按文字长度形成不同年份或类别组。"""
    members = [_LineItem(str(i), (10, 10 + i * 8, 15, 16 + i * 8), 0, i, effective_height=6) for i in range(8)]
    for column, (text, length) in enumerate([("Longest category", 70), ("Short", 25), ("Middle label", 50)]):
        members.append(
            _LineItem(text, (50 + column * 30, 90, 56 + column * 30, 90 + length), 270, 8 + column, effective_height=6)
        )
    content = _image_members_to_content(members, (200, 200))
    assert content.index("Longest category") < content.index("Short") < content.index("Middle label")
