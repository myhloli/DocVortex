"""独立改变位置、字号、字体与文字，验证十月八日规则的正反例边界。"""

from dataclasses import replace
import pytest
from _flash_pdf_test_utils import _text_line
from docvortex.analyzers.native.pdf.auxiliary_text import _marginal_text_matches
from docvortex.analyzers.native.pdf.code_blocks import _rule_delimited_code_members_are_structured
from docvortex.analyzers.native.pdf.index_blocks import _extract_index_blocks
from docvortex.analyzers.native.pdf.formulas import _recover_number_anchored_inline_display, order_equation_markers
from docvortex.analyzers.native.pdf.title_analysis.native_boundaries import (
    classify_bilingual_term_bands,
    suppress_continuous_sentence_titles,
    order_leading_title_bands,
)
from docvortex.analyzers.native.pdf._table_recovery import NativeTableInput, NativeTableRule
from docvortex.analyzers.native.pdf._table_recovery.text import build_native_table_text
from docvortex.analyzers.native.pdf._table_recovery.aligned_numeric import regroup_numeric_text, build_aligned_numeric_candidate


@pytest.mark.parametrize("scale,shift,font", [(0.7, 17, "RandomA"), (1, 80, "RandomB"), (1.6, 41, "UnknownFont")])
def test_bilingual_term_band_uses_member_geometry(scale, shift, font):
    """双语短题名需绑定条目号，定义和来源说明保持正文身份。"""
    raw = [
        ("8.23", (10, 10, 30, 20)),
        ("人工术语", (30, 25, 80, 35)),
        ("different term", (90, 25, 200, 35)),
        ("这是与先前原文无关的完整定义。", (30, 45, 230, 55)),
        ("[来源：某个标准]", (30, 60, 130, 70)),
    ]
    lines = [
        _text_line(t, tuple(x * scale + shift for x in box), i, effective_height=10 * scale, font_signature=(font, 0))
        for i, (t, box) in enumerate(raw)
    ]
    classify_bilingual_term_bands(lines)
    assert all(line.semantic_type == "paragraph_title" for line in lines[:3])
    assert len({line.title_band_id for line in lines[:3]}) == 1
    assert all(line.semantic_type is None and line.title_suppressed for line in lines[3:])
    negative = [
        replace(
            line,
            semantic_type=None,
            title_band_id=None,
            title_suppressed=False,
            structural_title=False,
            explicit_section_title=False,
        )
        for line in lines
    ]
    negative[2].text = "不含双语的普通正文。"
    classify_bilingual_term_bands(negative)
    assert all(line.semantic_type is None for line in negative)


@pytest.mark.parametrize("text", ["4.1", "12.20.3", "30.7"])
def test_hierarchical_body_number_is_not_repeated_header(text):
    """数字屏蔽不匹配层级编号，带稳定标准文字的页眉仍可正常匹配。"""
    assert not _marginal_text_matches(text, "5.2")
    assert _marginal_text_matches("TEST 2025 - standard", "TEST 2026 - standard")


@pytest.mark.parametrize("scale", [0.8, 1, 1.5])
def test_indent_and_rules_need_actual_code_evidence(scale):
    """带框说明即使有三档缩进也不是代码；真实伪代码控制语句可形成代码。"""
    prose = ["普通说明应保持完整的文本段落。"] * 6
    lines = [
        _text_line(
            t,
            tuple(v * scale for v in (20 + (i % 3) * 12, 30 + i * 12, 135, 38 + i * 12)),
            i,
            effective_height=8 * scale,
            visual_row_id=i,
        )
        for i, t in enumerate(prose)
    ]
    assert not _rule_delimited_code_members_are_structured(lines, (0, 0, 180 * scale, 110 * scale), 8 * scale)
    for i, line in enumerate(lines):
        line.text = ["if x > 0:", "return y;", "else:", "for k in values:", "return k;", "end"][i]
    assert _rule_delimited_code_members_are_structured(lines, (0, 0, 180 * scale, 110 * scale), 8 * scale)


@pytest.mark.parametrize("scale,offset", [(0.8, 15), (1, 0), (1.4, 35)])
def test_sentence_continuation_does_not_demote_real_heading(scale, offset):
    """连续句的跨行续词阻止标题升级；明确章节标题和带段界的题名保留。"""
    line = _text_line(
        "这是独立改变文字的正文，说明某项参数并继续进",
        (30, 40, 250, 54),
        0,
        effective_height=14,
        semantic_type="paragraph_title",
    )
    following = _text_line("行验证，随后给出判断结果。", (10, 55, 180, 66), 1, effective_height=11)
    for item in [line, following]:
        item.bbox = tuple(v * scale + offset for v in item.bbox)
        item.effective_height *= scale
    suppress_continuous_sentence_titles([line, following])
    assert line.semantic_type is None and line.title_suppressed
    heading = replace(line, semantic_type="paragraph_title", title_suppressed=False, structural_title=True)
    suppress_continuous_sentence_titles([heading, following])
    assert heading.semantic_type == "paragraph_title"


@pytest.mark.parametrize("prose", [False, True])
def test_right_number_requires_display_math_and_unique_order(prose):
    """右侧编号只能确认纯数学主体，正文引用与目录页码不得成为公式。"""
    body = _text_line(
        "q = (u - v) / m" if not prose else "这是解释 q = u 的普通正文", (110, 100, 240, 112), 0, effective_height=12
    )
    marker = _text_line("……（C.23）", (280, 100, 390, 112), 1, effective_height=12)
    blocks, claimed = _recover_number_anchored_inline_display([body, marker], [], (400, 500))
    assert bool(blocks) is not prose
    if not prose:
        assert claimed == {0} and blocks[0]["bbox"] == body.bbox
        number = {"type": "text", "content": marker.text, "bbox": marker.bbox}
        assert order_equation_markers([number, blocks[0]]) == [blocks[0], number]
        assert not _recover_number_anchored_inline_display([body, marker], [body.bbox], (400, 500))[0]


def test_leading_title_constraint_keeps_parallel_body_order():
    """只纠正局部页首题名，左右栏原有块序列保持稳定。"""
    title = {"type": "paragraph_title", "bbox": (140, 20, 260, 40), "content": "题名"}
    section = {"type": "paragraph_title", "bbox": (20, 50, 90, 60), "content": "章节"}
    left = {"type": "text", "bbox": (20, 80, 190, 180), "content": "左栏"}
    right = {"type": "text", "bbox": (210, 80, 390, 180), "content": "右栏"}
    assert order_leading_title_bands([section, title, left, right], (400, 500)) == [title, section, left, right]
    preceding = {**left, "bbox": (20, 10, 190, 180)}
    assert order_leading_title_bands([preceding, section, title, right], (400, 500)) == [preceding, section, title, right]


def test_plain_text_title_suppression_survives_visual_line_rebuild():
    """正文标题反证独立于标题带；同行字符重建后仍禁止晋升来源说明。"""
    from docvortex.analyzers.native.pdf.line_merging import _inherit_unanimous_structural_membership

    original = _text_line("[来源：另一个标准]", (20, 30, 150, 40), 0, effective_height=10)
    original.title_suppressed = True
    rebuilt = replace(original, title_suppressed=False)
    _inherit_unanimous_structural_membership(rebuilt, [original])
    assert rebuilt.title_suppressed and rebuilt.title_band_id is None


def test_financial_columns_are_not_unheaded_index():
    """重复金额列排除目录回退，真正的单页码目录仍可被识别。"""
    lines = []
    for r in range(6):
        for col, text in enumerate([f"项目{r}", str(100 + r), str(200 + r), str(300 + r)]):
            x = [10, 190, 265, 350][col]
            lines.append(
                _text_line(text, (x, 30 + r * 20, x + 40, 40 + r * 20), r * 4 + col, effective_height=10, visual_row_id=r)
            )
    assert not _extract_index_blocks(lines, (400, 250), [])[0]
    contents = []
    for r in range(6):
        contents.extend(
            [
                _text_line(f"条目{r}", (10, 30 + r * 20, 160, 40 + r * 20), r * 2, effective_height=10, visual_row_id=r),
                _text_line(str(r + 1), (370, 30 + r * 20, 380, 40 + r * 20), r * 2 + 1, effective_height=10, visual_row_id=r),
            ]
        )
    assert _extract_index_blocks(contents, (400, 250), [])[0]


@pytest.mark.parametrize("scale,shift,font", [(0.8, 13, "OtherA"), (1, 0, "OtherB"), (1.3, 21, "Substitute")])
def test_aligned_numeric_grid_is_position_and_font_independent(scale, shift, font):
    """以独立字符和部分横线验证空值、负数与三金额列，不依赖真实文件的字体标识。"""
    chars = []
    for row, fields in enumerate(
        [
            ["名称", "当前", "此前", "变动"],
            ["条目甲", "(121)", "42", "-3"],
            ["条目乙", "123", "44", "-5"],
            ["条目丙", "125", "46", "-7"],
        ]
    ):
        for col, text in enumerate(fields):
            x = [15, 210, 280, 350][col] - 0.5 * 10 * len(text) if col else 15
            for char in text:
                chars.append(
                    {
                        "char": char,
                        "char_idx": len(chars),
                        "bbox": tuple(v * scale + shift for v in (x, 10 + row * 25, x + 5, 20 + row * 25)),
                        "font": {"name": font, "size": 10 * scale, "weight": 400},
                    }
                )
                x += 5
    rules = tuple(
        NativeTableRule(tuple(v * scale + shift for v in (0, y, 390, y + 0.5)), 0.5 * scale, "horizontal") for y in [0, 30, 110]
    )
    inp = NativeTableInput(
        (shift, shift, 390 * scale + shift, 110 * scale + shift), (500 * scale, 300 * scale), 0, tuple(chars), rules
    )
    text = regroup_numeric_text(build_native_table_text(inp))
    result = build_aligned_numeric_candidate(inp, text)
    assert result is not None and (result.rows, result.cols) == (4, 4)
    assert [cell.content for cell in result.cells[4:8]] == ["条目甲", "(121)", "42", "-3"]
    assert build_aligned_numeric_candidate(replace(inp, drawing_lines=()), text) is None
