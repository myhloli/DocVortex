"""覆盖全页表区提名、字形原子认领及图形余区保留。"""

from __future__ import annotations

from io import BytesIO

import pytest
from reportlab.pdfgen.canvas import Canvas

from docvortex import parse
from docvortex.analyzers.native.pdf import pipeline
from docvortex.analyzers.native.pdf.models import _LineItem, _VerifiedTableCandidate
from docvortex.analyzers.native.pdf._table_recovery.contracts import (
    NativeTableCell,
    NativeTableGlyph,
    NativeTableInput,
    NativeTableResult,
    NativeTableRule,
    NativeTableText,
)
from docvortex.analyzers.native.pdf.table_materialization import (
    _is_verified_table_region,
    _stripe_cells_cross_gutters,
    _stripe_currency_crosses_column_rules,
)
from docvortex.analyzers.native.pdf.table_regions import (
    _TextBand,
    _repeated_numeric_gutters,
    _split_graphic_at_independent_grids,
)
from docvortex.render._internal.common.html_table import parse_html_tables

_LABEL_ROWS = (
    ("Application received", "8 May 2023"),
    ("Reviewed by committee", "11 May 2023"),
    ("Final decision", "18 May 2023"),
    ("Effective from", "6 June 2023"),
)
_BEFORE = "Introductory text above the records."
_AFTER = "The following paragraph is independent."


def _label_pdf(font: str, size: float, left: float, right: float, spacing: float, aligned: bool) -> bytes:
    """生成无横线的标签值区，独立改变页面位置、栏宽和字号。"""
    stream = BytesIO()
    pdf = Canvas(stream, pagesize=(540, 600))
    pdf.setFont(font, size)
    pdf.drawString(left - 27, 530, _BEFORE)
    for row, (label, value) in enumerate(_LABEL_ROWS):
        y = 473 - row * spacing * size
        pdf.drawString(left, y, label)
        pdf.drawRightString(right + (size if row == 2 and not aligned else 0), y, value)
    pdf.drawString(left - 27, 473 - (len(_LABEL_ROWS) + 0.6) * spacing * size, _AFTER)
    pdf.showPage()
    pdf.save()
    return stream.getvalue()


def _striped_pdf(with_gutter: bool, tail: str = "") -> tuple[bytes, tuple[tuple[str, str], ...]]:
    """把相接色块和无底纹首行印入真实 PDF，可替换成单栏说明反例。"""
    stream = BytesIO()
    pdf = Canvas(stream, pagesize=(540, 600))
    left, right, y, pitch = 73, 466, 471, 15
    pdf.setFont("Helvetica", 8.5)
    pdf.drawString(left, y + 34, _BEFORE)
    rows = (
        ("Period", "Amount received"),
        ("2020", "4,502"),
        ("2021", "7,198"),
        ("2022", "5,770"),
        ("2023", "6,499"),
        ("2024", "4,052"),
    )
    if tail:
        rows += (("2025", "8,304"),)
    for index, (label, value) in enumerate(rows):
        baseline = y - index * pitch
        if index % 2:
            pdf.setFillColorRGB(0.79, 0.91, 0.96)
            for start, end in ((left, 379), (379, 382), (382, right)):
                pdf.rect(start, baseline - 4, end - start, pitch, stroke=0, fill=1)
        pdf.setFillColorRGB(0.0, 0.0, 0.0)
        if with_gutter:
            pdf.drawString(left + 1, baseline, label)
            pdf.drawRightString(right - 3, baseline, value)
        else:
            pdf.drawString(left, baseline, f"Summary text for period {label}: {value}.")
    if tail == "closed":
        pdf.line(left, y - (len(rows) - 1) * pitch - 2.3, right, y - (len(rows) - 1) * pitch - 2.3)
    pdf.drawString(left, y - len(rows) * pitch - 11, _AFTER)
    pdf.showPage()
    pdf.save()
    return stream.getvalue(), rows


def _nested_grid_pdf(complete: bool, *, separate_frame: bool = False) -> tuple[bytes, tuple[tuple[str, ...], ...]]:
    """绘制独立闭合网格、图形余区及缺格反例。"""
    stream = BytesIO()
    pdf = Canvas(stream, pagesize=(540, 600))
    columns = (80, 169, 313, 432)
    top, bottom = 400, 358
    grid = pdf.beginPath()
    grid.rect(columns[0], bottom, columns[-1] - columns[0], top - bottom)
    if separate_frame:
        pdf.drawPath(grid)
        grid = pdf.beginPath()
    for column in columns[1:-1]:
        grid.moveTo(column, bottom)
        grid.lineTo(column, top)
    grid.moveTo(columns[0], (top + bottom) / 2)
    grid.lineTo(columns[-1], (top + bottom) / 2)
    pdf.drawPath(grid)
    pdf.setFont("Helvetica", 9)
    pdf.drawString(85, 410, _BEFORE)
    rows = (("Area", "Range of motion", "Rate"), ("Platform", "24 to 35 mm", "12 Hz"))
    for row, values in enumerate(rows):
        for column, text in enumerate(values):
            if row == 0 or column != 2 or complete:
                center = (columns[column] + columns[column + 1]) / 2
                pdf.drawCentredString(center, 386 - 21 * row, text)
    pdf.roundRect(81, 190, 350, 130, 3)
    pdf.drawString(95, 285, _AFTER)
    pdf.drawString(95, 266, "Independent original vector object.")
    pdf.showPage()
    pdf.save()
    return stream.getvalue(), rows


def _parse_with_region_evidence(source: bytes, monkeypatch):
    """走公开解析并观察新候选是否完成结构验证与整行认领。"""
    attempts = []
    original = pipeline._materialize_table_blocks

    def observe(source, candidates, **kwargs):
        """保存候选行集合及其对应的实际输出。"""
        out = original(source, candidates, **kwargs)
        attempts.extend(
            (candidate, candidate.line_indices <= out[2])
            for candidate in candidates
            if isinstance(candidate, _VerifiedTableCandidate)
        )
        return out

    monkeypatch.setattr(pipeline, "_materialize_table_blocks", observe)
    result = parse(source, file_suffix="pdf", parse_mode="txt", pdf_structure="auto", keep_model_json=True)
    return result, attempts


def _model_table_values(result):
    """取已认领表格中的值与跨度，排除普通文本中偶然出现的同词。"""
    tables = [block["content"] for block in result.model_json.to_dict()["pages"][0] if block["type"] == "table"]
    assert len(tables) == 1
    [grid] = parse_html_tables(tables[0])
    assert len(grid.cells) == grid.row_count * grid.column_count
    assert all(cell.rowspan == cell.colspan == 1 for cell in grid.cells)
    values = {(cell.row, cell.column): cell.tag.get_text() for cell in grid.cells}
    return tuple(tuple(values[row, col] for col in range(grid.column_count)) for row in range(grid.row_count))


@pytest.mark.parametrize("font,size,left,right", [("Times-Italic", 10, 120, 380), ("Helvetica", 12, 78, 420)])
def test_unruled_label_values_keep_source_geometry_and_custody(font, size, left, right, monkeypatch):
    """不同字体和栏宽下，孤立的对齐标签区都通过公开入口保全值及周边文字。"""
    result, attempts = _parse_with_region_evidence(_label_pdf(font, size, left, right, 2.1, True), monkeypatch)
    assert len(attempts) == 1 and attempts[0][1]
    assert _model_table_values(result) == _LABEL_ROWS
    blocks = result.to_dict()["pages"][0]["blocks"]
    text = str([block for block in blocks if block["type"] != "table"])
    assert _BEFORE in text and _AFTER in text
    assert all(label not in text and value not in text for label, value in _LABEL_ROWS)


@pytest.mark.parametrize("spacing,aligned", [(1.25, True), (2.1, False)])
def test_unruled_label_values_require_isolated_stable_rows(spacing, aligned, monkeypatch):
    """正文行距或移位值列不进入标签表识别。"""
    _, attempts = _parse_with_region_evidence(_label_pdf("Times-Roman", 10, 112, 405, spacing, aligned), monkeypatch)
    assert not attempts


@pytest.mark.parametrize("with_gutter", [True, False])
def test_repeated_row_fills_require_column_evidence(with_gutter, monkeypatch):
    """规则底纹本身不能证明表格，列沟和原生格值必须同时成立。"""
    pdf, rows = _striped_pdf(with_gutter)
    result, attempts = _parse_with_region_evidence(pdf, monkeypatch)
    if with_gutter:
        assert len(attempts) == 1 and attempts[0][1]
        assert _model_table_values(result) == rows
    else:
        assert not attempts


@pytest.mark.parametrize("tail", ["closed", "missing-edge"])
def test_unshaded_terminal_record_needs_printed_closing_edge(tail, monkeypatch):
    """最后一条同列同节距的记录随闭合底边入表，缺底边时撤销底纹候选。"""
    pdf, rows = _striped_pdf(True, tail)
    result, attempts = _parse_with_region_evidence(pdf, monkeypatch)
    if tail == "closed":
        assert len(attempts) == 1 and attempts[0][1]
        assert _model_table_values(result) == rows
    else:
        assert not attempts
    model = result.model_json.to_dict()["pages"][0]
    assert _AFTER in str(model)


def test_only_independent_cross_width_grid_splits_vector_container():
    """只有贯穿原图宽度且完整落在其中的表才移走对应区间。"""
    graphic = (10.0, 20.0, 280.0, 330.0)
    beside = (315.0, 20.0, 540.0, 240.0)
    unrelated_small_grid = (65.0, 30.0, 190.0, 70.0)
    independent_grid = (11.0, 85.0, 279.0, 150.0)
    assert _split_graphic_at_independent_grids([graphic, beside], [unrelated_small_grid, independent_grid]) == [
        (10.0, 20.0, 280.0, 85.0),
        (10.0, 150.0, 280.0, 330.0),
        beside,
    ]


@pytest.mark.parametrize("complete", [True, False])
@pytest.mark.parametrize("separate_frame", [False, True])
def test_independent_closed_grid_recovers_from_vector_collision(complete, separate_frame, monkeypatch):
    """已验证紧凑网格移出合成图形，多列旁白、有空格的图仍交给原归属。"""
    graphic = (79.0, 187.0, 433.0, 412.0)
    monkeypatch.setattr(pipeline, "_detect_strong_graphic_bboxes", lambda source: [graphic])
    source, rows = _nested_grid_pdf(complete, separate_frame=separate_frame)
    result, attempts = _parse_with_region_evidence(source, monkeypatch)
    blocks = result.model_json.to_dict()["pages"][0]
    content = str([block for block in blocks if block["type"] != "table"])
    assert _BEFORE in content and _AFTER in content
    assert len(attempts) == 1 and attempts[0][1] is complete
    if complete:
        assert _model_table_values(result) == rows
        [table] = [block for block in blocks if block["type"] == "table"]
        assert all(block["bbox"][1] >= table["bbox"][3] for block in blocks if block["type"] == "image")
    else:
        assert not any(block["type"] == "table" for block in blocks)


@pytest.mark.parametrize("continuous,changed_owner", [(True, True), (False, True), (True, False)])
def test_verified_region_rejects_characters_assigned_across_physical_column(continuous, changed_owner):
    """金额符号在相邻列时持续实体列线否决错位，短装饰线和正确落格保持通过。"""
    glyphs = (
        NativeTableGlyph(0, 101, "4,012", (33, 5, 51, 12), 0),
        NativeTableGlyph(1, 102, "$", (58, 5, 61, 12), 0),
        NativeTableGlyph(2, 103, "128", (71, 5, 80, 12), 0),
    )
    boxes = ((0, 0, 57, 20), (57, 0, 90, 20))
    cells = (
        NativeTableCell(0, 0, 1, 1, boxes[0], "4,012 $" if changed_owner else "4,012", (101, 102) if changed_owner else (101,)),
        NativeTableCell(0, 1, 1, 1, boxes[1], "128" if changed_owner else "$ 128", (103,) if changed_owner else (102, 103)),
    )
    result = NativeTableResult("", 1, 2, cells, NativeTableText(glyphs, (), 4, 7), "sparse_hybrid", 0.9)
    rule = NativeTableRule((65, 91, 66, 110 if continuous else 98), 1, "vertical")
    body = NativeTableInput((10, 90, 100, 110), (540, 600), 0, (), (rule,))
    assert _is_verified_table_region(body, result, 1, 0, frozenset({101, 102, 103})) is (not continuous or not changed_owner)
    assert not _is_verified_table_region(body, result, 1, 0, frozenset({101, 103}))


@pytest.mark.parametrize("middle_text,should_rollback", [("$", True), ("Actual words", False)])
def test_distinct_fields_must_stay_across_blank_gutter(middle_text, should_rollback):
    """跨过稳定空白的独立标签和值不能同格，连续长文字不视为跨列错位。"""
    glyphs = (
        NativeTableGlyph(0, 1, "Label", (6, 5, 25, 12), 0),
        NativeTableGlyph(1, 2, middle_text, (75, 5, 85, 12), 0),
        NativeTableGlyph(2, 3, "2,500", (119, 5, 135, 12), 0),
    )
    cell = NativeTableCell(0, 0, 1, 2, (0, 0, 145, 20), f"Label {middle_text} 2,500", (1, 2, 3))
    recovered = NativeTableResult("", 1, 2, (cell,), NativeTableText(glyphs, (), 4, 7), "vector_grid", 0.9)
    body = NativeTableInput((10, 90, 155, 110), (540, 600), 0, ())
    assert _stripe_cells_cross_gutters(body, recovered, ((36, 127),)) is should_rollback


@pytest.mark.parametrize("crossing_y,should_accept", [(6, True), (32, False)])
def test_body_rail_can_start_below_a_printed_header(crossing_y, should_accept):
    """额外表头不稀释正文列线覆盖率，只有实体列线实际穿过的金额落格需要否决。"""
    glyphs = (
        NativeTableGlyph(0, 1, "190", (22, crossing_y, 34, crossing_y + 7), 0),
        NativeTableGlyph(1, 2, "$", (60, crossing_y, 64, crossing_y + 7), 0),
        NativeTableGlyph(2, 3, "490", (75, crossing_y, 88, crossing_y + 7), 0),
    )
    cells = (
        NativeTableCell(0, 0, 1, 1, (0, 0, 58, 80), "190 $", (1, 2)),
        NativeTableCell(0, 1, 1, 1, (58, 0, 95, 80), "490", (3,)),
    )
    result = NativeTableResult("", 1, 2, cells, NativeTableText(glyphs, (), 4, 7), "sparse_hybrid", 0.9)
    rule = NativeTableRule((65, 115, 66, 167), 1, "vertical")
    body = NativeTableInput((10, 90, 105, 170), (540, 600), 0, (), (rule,))
    indices = frozenset({1, 2, 3})
    assert _is_verified_table_region(body, result, 1, 0, indices)
    assert _is_verified_table_region(body, result, 1, 0, indices, (114, 168)) is should_accept


@pytest.mark.parametrize("distorted_rows,expect_numeric_gaps", [(1, True), (3, False)])
def test_repeat_value_tracks_allow_nonconforming_total_rows(distorted_rows, expect_numeric_gaps):
    """金额列在多数正常行保持右缘时仍成立，仅偶发并列数字不提供列界。"""
    rows = []
    for index in range(5):
        y = 180 + 15 * index
        shift = index * 9 if index >= 5 - distorted_rows else 0
        numbers = [
            ("1,234", (255, y, 278, y + 10)),
            ("2,345", (311 + shift, y, 335 + shift, y + 10)),
            ("3,456", (360 + shift, y, 386 + shift, y + 10)),
        ]
        lines = [_LineItem(text, box, 0, col) for col, (text, box) in enumerate(numbers)]
        rows.append(_TextBand((30, y, 386 + shift, y + 10), lines))
    assert bool(_repeated_numeric_gutters(rows, 15)) is expect_numeric_gaps


@pytest.mark.parametrize("kind", ["wrong-owner", "proper-prefix", "proper-suffix", "no-distinct-rules"])
def test_stripe_currency_ownership_uses_independent_printed_numeric_lanes(kind):
    """只有短横轨及重复金额轨共同证明归属时撤销错挂，保留前置、后置的合法货币。"""
    glyphs = []
    cells = []
    for row in range(3):
        y = 9 + 16 * row
        series = (
            ("23", (31, y, 41, y + 8)),
            ("$", (45 if kind == "proper-suffix" else 68, y, 49 if kind == "proper-suffix" else 73, y + 8)),
            ("1560", (93, y, 111, y + 8)),
        )
        ids = [row * 3 + n for n in range(3)]
        glyphs.extend(NativeTableGlyph(index, index, text, box, row) for index, (text, box) in zip(ids, series))
        old_owner = kind != "proper-prefix"
        cells.extend(
            (
                NativeTableCell(
                    row, 0, 1, 1, (15, y, 60, y + 10), "23 $" if old_owner else "23", tuple(ids[:2] if old_owner else ids[:1])
                ),
                NativeTableCell(
                    row,
                    1,
                    1,
                    1,
                    (65, y, 113, y + 10),
                    "1560" if old_owner else "$ 1560",
                    tuple(ids[2:] if old_owner else ids[1:]),
                ),
            )
        )
    rules = (
        NativeTableRule((25, 86, 70, 87), 1, "horizontal"),
        NativeTableRule((75, 86, 125, 87), 1, "horizontal"),
    )
    if kind == "no-distinct-rules":
        rules = (NativeTableRule((25, 86, 125, 87), 1, "horizontal"),)
    body = NativeTableInput((10, 90, 140, 150), (540, 600), 0, (), rules)
    result = NativeTableResult("", 3, 2, tuple(cells), NativeTableText(tuple(glyphs), (), 4, 8), "sparse_grid", 0.9)
    assert _stripe_currency_crosses_column_rules(body, result, (90, 150)) is (kind == "wrong-owner")
