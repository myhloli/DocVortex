"""检查来源基线支撑的伪代码投影及真实公开 P050。"""

from pathlib import Path
import os
import re

from bs4 import BeautifulSoup
import pytest

from docvortex import parse
from docvortex.api import render
from docvortex.analyzers.native.pdf.algorithm_projection import _group_algorithm_rows, project_numbered_algorithm
from docvortex.analyzers.native.pdf.formulas import _split_visual_row_has_prose_continuation
from docvortex.analyzers.native.pdf.models import _LineItem, _TextLane
from docvortex.content.spans import inline_span_plain_text
from docvortex.render.contracts import HtmlRenderOptions


def _row_glyphs(scale, start, baseline, text, first, font_scale=1.0):
    """按比例排版的实际墨迹坐标构造一个真实基线同行，无固定页面尺寸。"""
    chars = []
    x = start
    for glyph in text:
        if glyph == " ":
            x += 0.29 * scale
            continue
        width = 0.45 * scale * font_scale
        box = (x, baseline - 0.7 * scale * font_scale, x + width, baseline + 0.05 * scale)
        chars.append(
            {
                "char_idx": first + len(chars),
                "char": glyph,
                "origin": (x, baseline),
                "bbox": box,
                "tight_bbox": box,
                "font": {"size": scale * font_scale, "name": "Varied Source Typeface"},
                "is_generated": False,
            }
        )
        x += 0.52 * scale * font_scale
    return chars, x


def _algorithm_glyphs(scale, x, y, *, complex_expression=False, literal=False):
    """把撇号和下标放在同一程序变量的不同基线，独立设置测试对照。"""
    glyphs = []

    def append(x, baseline, text, font_scale=1.0):
        """在测试区域添加真实字形并保留全局唯一索引。"""
        group, right = _row_glyphs(scale, x, baseline, text, len(glyphs), font_scale)
        glyphs.extend(group)
        return right

    append(x, y, "Algorithm 12: Traverse(S)")
    for number, indent, statement in (
        (7, 0, "while x"),
        (8, 1, "if Valid(x) then"),
        (9, 2, "return x;" if not literal else 'return "two  spaces";'),
        (10, 0, "return 0;"),
    ):
        baseline = y + (number - 6) * 1.28 * scale
        if number == 10:
            append(x - 0.52 * scale, baseline, str(number))
        else:
            append(x, baseline, str(number))
        end = append(x + (2 + 1.4 * indent) * scale, baseline, statement)
        if number == 7:
            delta = 0.59 if complex_expression else 0.25
            append(end, baseline - delta * scale, "′", 0.7)
            append(end, baseline + 0.21 * scale, "goal", 0.7)
            append(end + 1.7 * scale, baseline, "do")
    bbox = (x - scale, y - scale, x + 24 * scale, y + 7 * scale)
    return glyphs, bbox


_LEGACY = "Algorithm 12: Traverse(S)\n 7 while x ′\n            goal do\n 8  if Valid(x) then\n 9   return x;\n10 return 0;"


@pytest.mark.parametrize(("scale", "x", "y"), [(9.0, 42.0, 104.0), (13.0, 187.0, 297.0)])
def test_numbered_algorithm_preserves_actual_rows_indents_and_numeric_zero(scale, x, y):
    """平移和独立变字号后仍按物理编号行归属，上下标仅留在当前变量。"""
    glyphs, bbox = _algorithm_glyphs(scale, x, y)
    result = project_numbered_algorithm(glyphs, bbox, 0, _LEGACY)
    assert result.splitlines() == [
        "Algorithm 12: Traverse(S)",
        " 7 while x′_{goal} do",
        " 8   if Valid(x) then",
        " 9     return x;",
        "10 return 0;",
    ]


@pytest.mark.parametrize("variant", ["literal", "complex_expression", "rotated", "without_heading"])
def test_numbered_algorithm_refuses_unsafe_reprojection(variant):
    """保护真正跨带公式、字符串字距、旋转及缺少算法题名的页面。"""
    glyphs, bbox = _algorithm_glyphs(
        9, 42, 104, complex_expression=variant == "complex_expression", literal=variant == "literal"
    )
    source = _LEGACY.removeprefix("Algorithm 12: Traverse(S)\n") if variant == "without_heading" else _LEGACY
    assert project_numbered_algorithm(glyphs, bbox, 90 if variant == "rotated" else 0, source) == source


@pytest.mark.parametrize("midpoint", [False, True])
def test_numbered_algorithm_search_handles_boundaries_and_rejects_ties(midpoint):
    """长清单首尾两侧仍能找到唯一基线，两个可归属基线等距时拒绝。"""
    bases = [50.0 + index * 9 for index in range(85)]
    markers = [(baseline, [{"char_idx": index, "char": "1"}]) for index, baseline in enumerate(bases)]
    heading = {"char_idx": 85, "char": "A", "origin": (20.0, 31.0)}
    body = [
        {"char_idx": 86 + index, "char": "x", "origin": (20.0, baseline + (4 if index == 84 else -4 if index == 0 else 0))}
        for index, baseline in enumerate(bases)
    ]
    if midpoint:
        body[42]["origin"] = (20.0, (bases[42] + bases[43]) / 2)
        assert _group_algorithm_rows([heading, *body], markers, 10.0) is None
    else:
        baseline, rows = _group_algorithm_rows([heading, *body], markers, 10.0)
        assert baseline == 31.0
        assert rows == [[(glyph, False)] for glyph in [heading, *body]]


@pytest.mark.parametrize(
    ("row_word", "neighbor", "neighbor_y", "expected"),
    [
        ("route", "∩ x = ∅", 72.0, True),
        ("route", "normally", 72.0, False),
        ("route", "∩ x = ∅", 101.0, False),
        ("u=v", "∩ x = ∅", 72.0, False),
    ],
)
def test_split_prose_words_hold_the_physical_row_against_formula_anchors(row_word, neighbor, neighbor_y, expected):
    """附近确有独立数学行时才保护拆开的正文，纯正文和真正数学正文保持原路径。"""
    entries = [
        ("Provided", (30.0, 60.0, 65.0, 70.0), 3),
        (row_word, (85.0, 60.0, 110.0, 70.0), 3),
        ("remains", (136.0, 60.1, 173.0, 70.1), 3),
        ("continuous", (200.0, 60.0, 255.0, 70.0), 3),
        ("under", (30.0, 72.0, 55.0, 82.0), 4),
        (neighbor, (70.0, neighbor_y, 95.0, neighbor_y + 10.0), 4),
    ]
    lines = [
        _LineItem(text, bbox, 0, idx, visual_row_id=row, split_from_row=True, effective_height=10.0)
        for idx, (text, bbox, row) in enumerate(entries)
    ]
    lane = _TextLane(left=30.0, right=255.0, lines=[(line, line.bbox) for line in lines])
    assert _split_visual_row_has_prose_continuation(lane, lines[3], lines[:4], 10.0) is expected


def test_rrt_original_public_numbered_rows():
    """对公开原件断言三组独立清单、关键符号、真实语句数和四级缩进。"""
    path = os.getenv("DOCVORTEX_TEST_RRT_ALGORITHM_PDF")
    if not path:
        pytest.skip("DOCVORTEX_TEST_RRT_ALGORITHM_PDF is not configured")
    result = parse(Path(path), parse_mode="txt", keep_model_json=True)
    assert len(result.model_json.pages) == 21
    code = [block["content"] for block in result.model_json.pages[4] if block["type"] == "code"]
    assert [len(block.splitlines()) for block in code] == [8, 5, 8]
    assert all("x0" not in block and "x^{0}" not in block for block in code)
    assert sum(block.count("′") for block in code) == 13
    assert re.search(r"^4 {7}x′_\{parent\} ← GetParent\(E, ?x′\);$", code[2], re.M)
    assert "if (c(x_{rand}) + c(τ)) < c(x′) then" in code[2]
    output = render(result.middle_json, "html", assets=result.assets, options=HtmlRenderOptions(standalone=False))
    soup = BeautifulSoup(output.content, "html.parser", from_encoding="utf-8")
    assert all(block in [node.get_text() for node in soup.select("pre code")] for block in code)


def test_rrt_original_theorem_keeps_prose_and_real_standalone_formula():
    """验证真实字体修复后仍保留定理前两行，独立分式继续由展示式框输出。"""
    path = os.getenv("DOCVORTEX_TEST_RRT_ALGORITHM_PDF")
    if not path:
        pytest.skip("DOCVORTEX_TEST_RRT_ALGORITHM_PDF is not configured")
    result = parse(Path(path), parse_mode="txt", keep_model_json=True)
    page = result.model_json.pages[8]
    equations = [block["bbox"] for block in page if block["type"] == "equation"]
    assert len(equations) == 1 and 0.51 < equations[0][1] < equations[0][3] < 0.56
    prose = [inline_span_plain_text(block["content"]) for block in page if block["type"] == "text"]
    assert any("Theorem 3 [6] Given a path planning" in block for block in prose)
    assert any("problem {Xfree, xinit, Xgoal}, if there exits" in block for block in prose)
