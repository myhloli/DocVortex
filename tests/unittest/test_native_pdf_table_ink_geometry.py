"""验证字体布局框虚高时的语言无关表格组行与字符守恒。"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import replace
import json
import math
from pathlib import Path

from bs4 import BeautifulSoup
import pytest

from docvortex.analyzers.pdf import PDFTablePage, recover_table_region
from docvortex.analyzers.native.pdf._table_recovery.contracts import NativeTableInput, NativeTableRectangle, NativeTableRule
from docvortex.analyzers.native.pdf._table_recovery.engine import recover_native_pdf_table
from docvortex.analyzers.native.pdf._table_recovery import text as table_text
from docvortex.document.pdf import PDFPageTextGeometry


def _real_input() -> NativeTableInput:
    """读取原件首表的原始区域证据，保留来源指纹和全部几何。"""
    path = Path(__file__).parents[1] / "fixtures/native_pdf_table_inflated_ink.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["source_sha256"] == "0ccb13c1c57dbfa43903ff64e9164c6693ac38a879886dbdb7b2204d4e815daa"
    return NativeTableInput(
        table_bbox=tuple(data["bbox"]),
        page_size=tuple(data["page_size"]),
        angle=0,
        chars=tuple(data["chars"]),
        drawing_lines=tuple(NativeTableRule(**value) for value in data["drawing_lines"]),
        rectangles=tuple(NativeTableRectangle(**value) for value in data["rectangles"]),
    )


def _public_page(source: NativeTableInput) -> PDFTablePage:
    """构造与公开页面准备接口一致的原生几何视图。"""
    return PDFTablePage(
        source.page_size,
        PDFPageTextGeometry(
            chars=list(source.chars),
            tight_bboxes={c["char_idx"]: tuple(c["tight_bbox"]) for c in source.chars if c.get("tight_bbox")},
            origins={c["char_idx"]: tuple(c["origin"]) for c in source.chars if c.get("origin")},
        ),
        source.drawing_lines,
        source.rectangles,
    )


@pytest.mark.parametrize("adapter", [False, True])
def test_real_table_restores_rows_without_losing_punctuation_or_scripts(monkeypatch: pytest.MonkeyPatch, adapter: bool) -> None:
    """真实表格须恢复完整语序、独立末行及负号，并保持字符唯一归属。"""
    if adapter:
        monkeypatch.setattr(table_text, "_select_pending_glyphs", table_text._select_pending_glyphs_adapter)
    source = _real_input()
    before = deepcopy(source)
    pending = table_text._select_pending_glyphs(source)
    result = recover_native_pdf_table(source)
    assert result is not None
    assert (result.rows, result.cols) == (11, 3)
    assert len(pending) == len(result.text.glyphs) == 452
    expected_members = Counter(g.source_index for g in pending)
    assert Counter(g.source_index for g in result.text.glyphs) == expected_members
    assert Counter(i for cell in result.cells for i in cell.source_char_indices) == expected_members
    public = recover_table_region(_public_page(source), source.table_bbox)
    assert public is not None
    rows = BeautifulSoup(public.html, "html.parser").find_all("tr")
    assert len(rows) == 11 and all(len(row.find_all("td")) == 3 for row in rows)
    assert rows[4].find_all("td")[0].get_text() == ("Frequency Tolerance over Operating Temperature Range 频率温度特性")
    assert rows[9].find_all("td")[0].get_text() == "Insulation Resistance 绝缘电阻"
    assert rows[10].find_all("td")[0].get_text() == "Aging 老化率"
    assert rows[5].find_all("td")[2].get_text().count("-20℃") == 2
    assert "-55℃" in rows[6].find_all("td")[2].get_text()
    for row, base, subscript in ((1, "f", "0"), (3, "C", "L"), (5, "T", "OPR"), (6, "T", "STG"), (8, "C", "0")):
        symbol = rows[row].find_all("td")[1]
        assert symbol.get_text() == base + subscript
        assert symbol.find("sub").get_text() == subscript
    assert source == before


def _two_line_input(
    rows: tuple[str, str] = ("AB", "CD"),
    *,
    scale: float = 1.0,
    offset: tuple[float, float] = (0.0, 0.0),
    spacing: float = 6.0,
    angle: int = 0,
) -> NativeTableInput:
    """构造少量字母或数字即可证明误合行的原始框、墨迹与基线。"""
    size = 100.0 * scale

    def point(x: float, y: float) -> tuple[float, float]:
        """把正向测试点逆变换到整表旋转前的页面坐标。"""
        x, y = (x + offset[0]) * scale, (y + offset[1]) * scale
        return {0: (x, y), 90: (size - y, x), 180: (size - x, size - y), 270: (y, size - x)}[angle]

    def box(x0: float, y0: float, x1: float, y1: float) -> tuple[float, float, float, float]:
        """用角点构造合法旋转框，保持测试字体与源序号独立。"""
        points = (point(x0, y0), point(x1, y1))
        return min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)

    chars = []
    for row_index, text in enumerate(rows):
        top = 10.0 + row_index * 10.0
        for column, value in enumerate(text):
            left = 10.0 + column * spacing
            chars.append(
                {
                    "char_idx": len(chars),
                    "char": value,
                    "bbox": box(left, top, left + spacing, top + 20.0),
                    "tight_bbox": box(left + 1.0, top + 8.0, left + spacing - 1.0, top + 13.0),
                    "origin": point(left, top + 13.0),
                    "writing_angle": math.radians(angle),
                    "font": {"name": "ArbitraryFont", "size": 1.0, "weight": 400, "flags": 0},
                }
            )
    return NativeTableInput((0.0, 0.0, size, size), (size, size), angle, tuple(chars))


@pytest.mark.parametrize("rows", [("AB", "CD"), ("12", "34"), ("中A", "3文")])
@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_short_rows_do_not_require_chinese_or_a_font_name(rows: tuple[str, str], angle: int) -> None:
    """四个锚字即可证实双行，不绑定汉字数量、字体名称或整表方向。"""
    source = _two_line_input(rows, angle=angle)
    before = deepcopy(source)
    text = table_text.build_native_table_text(source)
    assert text is not None
    assert ["".join(token.text for token in row.tokens) for row in text.rows] == list(rows)
    assert Counter(g.source_index for g in text.glyphs) == Counter(range(4))
    assert source == before


@pytest.mark.parametrize(
    "scale,offset,spacing", [(0.5, (0.0, 0.0), 6.0), (2.0, (0.0, 0.0), 6.0), (1.0, (15.0, 20.0), 6.0), (1.0, (0.0, 0.0), 14.0)]
)
def test_ink_geometry_generalizes_scale_position_and_line_width(
    scale: float, offset: tuple[float, float], spacing: float
) -> None:
    """分别变化字号、位置和行宽，仍按源事实恢复相同文本行。"""
    source = _two_line_input(scale=scale, offset=offset, spacing=spacing)
    text = table_text.build_native_table_text(source)
    assert ["".join(token.text for token in row.tokens) for row in text.rows] == ["AB", "CD"]


@pytest.mark.parametrize("fault", ["missing", "zero", "nan", "infinity", "duplicate_source", "mixed_direction"])
def test_incomplete_ink_evidence_keeps_the_whole_original_geometry(fault: str) -> None:
    """证据不完整时整表回退，不能仅缩小部分字形制造新的尺度混用。"""
    source = _two_line_input()
    chars = deepcopy(list(source.chars))
    if fault == "missing":
        chars[-1].pop("tight_bbox")
    elif fault == "zero":
        chars[-1]["tight_bbox"] = (10.0, 10.0, 10.0, 10.0)
    elif fault in {"nan", "infinity"}:
        chars[-1]["tight_bbox"] = (10.0, float("nan") if fault == "nan" else float("inf"), 15.0, 30.0)
    elif fault == "duplicate_source":
        chars[-1]["char_idx"] = 0
    else:
        chars[-1]["writing_angle"] = math.pi / 2
    source = replace(source, chars=tuple(chars))
    pending = table_text._select_pending_glyphs(source)
    text = table_text.build_native_table_text(source)
    assert [g.bbox for g in text.glyphs] == [g.bbox for g in pending]


@pytest.mark.parametrize("rows", [("--", ".."), ("AB", "2")])
def test_punctuation_and_isolated_scripts_cannot_trigger_calibration(rows: tuple[str, str]) -> None:
    """薄标点或孤立上下标不足以证明正文双行，保留原有字形框。"""
    source = _two_line_input(rows)
    pending = table_text._select_pending_glyphs(source)
    text = table_text.build_native_table_text(source)
    assert [g.bbox for g in text.glyphs] == [g.bbox for g in pending]


@pytest.mark.parametrize(
    "case", ["normal_height", "separate_rows", "separate_columns", "small_script", "normal_lower_band", "one_baseline"]
)
def test_valid_layouts_and_unproven_inflation_keep_original_geometry(case: str) -> None:
    """正文同行、异列错位和正常字号结构不能因为个别高框而被校准。"""
    source = _two_line_input()
    chars = deepcopy(list(source.chars))
    for index, char in enumerate(chars):
        x0, y0, x1, y1 = char["bbox"]
        if case == "normal_height":
            char["bbox"] = (x0, char["tight_bbox"][1] - 1.0, x1, char["tight_bbox"][3] + 1.0)
        elif index >= 2:
            if case in {"separate_rows", "separate_columns"}:
                dx, dy = (40.0, 0.0) if case == "separate_columns" else (0.0, 30.0)
                for key in ("bbox", "tight_bbox"):
                    a, b, c, d = char[key]
                    char[key] = (a + dx, b + dy, c + dx, d + dy)
                char["origin"] = (char["origin"][0] + dx, char["origin"][1] + dy)
            elif case == "small_script":
                a, b, c, d = char["tight_bbox"]
                char["tight_bbox"] = (a, d - 2.0, c, d)
            elif case == "normal_lower_band":
                char["bbox"] = (x0, 26.0, x1, 34.0)
            else:
                char["origin"] = (char["origin"][0], 23.0)
    source = replace(source, chars=tuple(chars))
    pending = table_text._select_pending_glyphs(source)
    text = table_text.build_native_table_text(source)
    assert [g.bbox for g in text.glyphs] == [g.bbox for g in pending]


@pytest.mark.parametrize("inflate", [2.0, 1.999])
def test_layout_inflation_boundary_requires_two_independent_ink_lines(inflate: float) -> None:
    """边界几何同时满足真实双行与原框重叠时，只有足够虚高才允许校准。"""
    source = _two_line_input()
    chars = deepcopy(list(source.chars))
    for index, char in enumerate(chars):
        row = index // 2
        top = 10.0 + row * 5.5
        x0, _, x1, _ = char["bbox"]
        char["bbox"] = (x0, top, x1, top + 5.0 * inflate)
        char["tight_bbox"] = (x0 + 1.0, top + 4.5, x1 - 1.0, top + 9.5)
        char["origin"] = (x0, top + 9.5)
    source = replace(source, chars=tuple(chars))
    pending = table_text._select_pending_glyphs(source)
    text = table_text.build_native_table_text(source)
    if inflate == 2.0:
        assert ["".join(token.text for token in row.tokens) for row in text.rows] == ["AB", "CD"]
        assert all(g.bbox[3] - g.bbox[1] == 5.0 for g in text.glyphs)
    else:
        assert [g.bbox for g in text.glyphs] == [g.bbox for g in pending]


def test_changing_font_metadata_cannot_change_real_table_recovery() -> None:
    """改写全部字体名称与声明字号，仍按相同几何恢复真实表格。"""
    source = _real_input()
    changed = []
    for index, char in enumerate(source.chars):
        changed.append(dict(char, font={"name": f"OtherFont{index % 3}", "size": 13.0, "weight": 500, "flags": 0}))
    original = recover_native_pdf_table(source)
    renamed = recover_native_pdf_table(replace(source, chars=tuple(changed)))
    assert renamed is not None and original is not None
    assert renamed.html == original.html
    assert renamed.cells == original.cells


def test_neighboring_column_bridge_does_not_prove_inflated_lines() -> None:
    """异列居中文字可桥接两条正常行，不能把整行外框的增长当成字体虚高。"""
    source = _two_line_input()
    chars = deepcopy(list(source.chars))
    for index, char in enumerate(chars):
        top = 10.0 + (index // 2) * 12.0
        left, _, right, _ = char["bbox"]
        char["bbox"] = (left, top, right, top + 10.0)
        char["tight_bbox"] = (left + 1.0, top + 4.0, right - 1.0, top + 8.0)
        char["origin"] = (left, top + 8.0)
    for index, value in enumerate("XY"):
        left = 40.0 + index * 6.0
        chars.append(
            {
                "char_idx": 4 + index,
                "char": value,
                "bbox": (left, 14.0, left + 6.0, 28.0),
                "tight_bbox": (left + 1.0, 19.0, left + 5.0, 24.0),
                "origin": (left, 24.0),
                "writing_angle": 0.0,
            }
        )
    source = replace(source, chars=tuple(chars))
    pending = table_text._select_pending_glyphs(source)
    assert len(table_text._assign_visual_rows(pending, 10.0)) == 1
    assert len(table_text._assign_visual_rows(pending[:4], 10.0)) == 2
    text = table_text.build_native_table_text(source)
    assert [g.bbox for g in text.glyphs] == [g.bbox for g in pending]


def test_real_bridged_table_keeps_its_existing_structure_and_geometry() -> None:
    """保留原件第31页的正常多行概率表，避免缩框导致已正确网格退回纯文本。"""
    path = Path(__file__).parents[1] / "fixtures/native_pdf_table_ink_bridge.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    source = NativeTableInput(
        tuple(data["table_bbox"]),
        tuple(data["page_size"]),
        data["angle"],
        tuple(data["chars"]),
        tuple(NativeTableRule(**item) for item in data["drawing_lines"]),
        tuple(NativeTableRectangle(**item) for item in data["rectangles"]),
    )
    pending = table_text._select_pending_glyphs(source)
    result = recover_native_pdf_table(source)
    assert result is not None and (result.rows, result.cols) == (7, 3)
    assert result.html == data["expected_html"]
    assert [glyph.bbox for glyph in result.text.glyphs] == [glyph.bbox for glyph in pending]
    assert [cell.content for cell in result.cells if cell.row == 2] == [
        "Probability of reaching the EXPLORE benchmark",
        "26",
        "71",
    ]


@pytest.mark.parametrize(
    "case,calibrated",
    [
        ("half_horizontal_overlap", True),
        ("less_horizontal_overlap", False),
        ("minimum_ink_gap", True),
        ("smaller_ink_gap", False),
        ("equal_height_limit", True),
        ("smaller_height_limit", False),
        ("overlapping_ink", False),
        ("close_baselines", False),
    ],
)
def test_independent_geometric_guards_do_not_relax_each_other(case: str, calibrated: bool) -> None:
    """用独立变动的源几何覆盖投影、留白、主体大小及基线边界。"""
    source = _two_line_input()
    chars = deepcopy(list(source.chars))
    for index, char in enumerate(chars):
        if case in {"equal_height_limit", "smaller_height_limit"} and index < 2:
            a, _, c, d = char["tight_bbox"]
            char["tight_bbox"] = (a, d - 6.0, c, d)
        if index < 2:
            continue
        a, b, c, d = char["tight_bbox"]
        if case in {"half_horizontal_overlap", "less_horizontal_overlap"}:
            dx = 5.0 if case == "half_horizontal_overlap" else 5.01
            x0, y0, x1, y1 = char["bbox"]
            char["bbox"] = (x0 + dx, y0, x1 + dx, y1)
            char["tight_bbox"] = (a + dx, b, c + dx, d)
            char["origin"] = (char["origin"][0] + dx, char["origin"][1])
        elif case in {"minimum_ink_gap", "smaller_ink_gap", "overlapping_ink"}:
            top = {"minimum_ink_gap": 23.5, "smaller_ink_gap": 23.49, "overlapping_ink": 22.0}[case]
            char["tight_bbox"] = (a, top, c, top + 5.0)
        elif case in {"equal_height_limit", "smaller_height_limit"}:
            height = 4.0 if case == "equal_height_limit" else 3.99
            char["tight_bbox"] = (a, d - height, c, d)
        else:
            char["origin"] = (char["origin"][0], 27.99)
    source = replace(source, chars=tuple(chars))
    pending = table_text._select_pending_glyphs(source)
    result = table_text.build_native_table_text(source)
    if calibrated:
        assert ["".join(token.text for token in row.tokens) for row in result.rows] == ["AB", "CD"]
        assert [glyph.bbox for glyph in result.glyphs] != [glyph.bbox for glyph in pending]
    else:
        assert [glyph.bbox for glyph in result.glyphs] == [glyph.bbox for glyph in pending]
