"""表身共同列缝与多层表头的正常入口、跨格文字和近似反例。"""

from io import BytesIO

import pytest
from bs4 import BeautifulSoup
from reportlab.pdfgen.canvas import Canvas

from docvortex import parse
from docvortex.analyzers.native.pdf._table_recovery.column_groups import build_column_group_candidate
from docvortex.analyzers.native.pdf._table_recovery.contracts import NativeTableInput, NativeTableRule
from docvortex.analyzers.native.pdf._table_recovery.text import build_native_table_text
from test_native_pdf_table import _char_items


def _grouped_table(mode="paired", dx=0, step=46, dy=0):
    """由独立可变的文本、列宽、坐标和局部横线构造两至三层表头。"""
    columns = [dx + (i + 0.5) * step for i in range(5)]
    width = 5 * step
    entries = []

    def write(center, y, text):
        """用实际字符串长度分配源字符，整体平移保持各项的独立位置。"""
        half = 1.65 * len(text)
        entries.append((text, (center - half, dy + y, center + half, dy + y + 5)))

    def rail(y, start, stop):
        """提供可单独移除或移动端点的物理横线。"""
        return NativeTableRule((dx + start, dy + y, dx + stop, dy + y + 0.2), 0.2, "horizontal")

    rules = [rail(y, 0, width) for y in (0, 37, 141)]
    if mode == "three":
        write((columns[1] + columns[4]) / 2, 3, "Results")
        for j in (1, 3):
            write((columns[j] + columns[j + 1]) / 2, 14, ("Front" if j == 1 else "Rear"))
    elif mode in {"wrapped", "ruled-odd"}:
        write(columns[2], 8, "Independent")
        if mode == "ruled-odd":
            rules.append(rail(19, 0.9 * step, 4.1 * step))
    else:
        write((columns[1] + columns[4]) / 2 + (0.42 * step if mode == "off-center" else 0), 8, "Results")
    if mode in {"wrapped", "ruled-odd"}:
        leafcols = range(5)
    else:
        leafcols = range(1, 5)
        write(columns[0], 16, "Name")
    for j in leafcols:
        write(columns[j], 28, ("Key", "A", "B", "C", "D")[j])
    if mode == "broken-rule":
        rules.append(rail(21, 1.3 * step, 2.7 * step))
    if mode == "physical-conflict":
        rules.append(NativeTableRule((columns[2], dy, columns[2] + 0.2, dy + 101), 0.2, "vertical"))
    if mode in {"section", "loose-single"}:
        write(columns[0], 68, "Scoped")
        if mode == "section":
            rules.append(rail(65, 0, width))
    for i, y in enumerate((42, 55, 82, 99, 116, 132)):
        data = ("Oak" if i % 2 else "Elm", "-1.50", "+2", "0", "8.00")
        for j, txt in enumerate(data):
            shift = 0.05 * step if mode == "loose-single" and j == 0 and y >= 82 else 0
            if mode == "overlap" and i == 2 and j == 1:
                shift = 0.95 * step
            write(columns[j] + shift, y, txt)
    return NativeTableInput((dx, dy, dx + width, dy + 141.2), (800, 700), 0, _char_items(entries), tuple(rules))


@pytest.mark.parametrize("dx,dy,step", [(0, 0, 46), (35, 57, 61), (81, 10, 37)])
@pytest.mark.parametrize("mode", ["paired", "three", "ruled-odd", "section"])
def test_data_columns_define_header_groups_and_preserve_values(mode, dx, dy, step):
    """按连续子列和物理线确定父组，验证完整层数、分节归属、正负号、尾零和字形唯一归属。"""
    table = _grouped_table(mode, dx, step, dy)
    candidate = build_column_group_candidate(table, build_native_table_text(table))
    assert candidate is not None
    rows = 3 if mode == "three" else 2
    cells = {(c.row, c.col): c for c in candidate.cells}
    if mode == "ruled-odd":
        assert cells[0, 1].colspan == 3
        assert cells[0, 0].rowspan == cells[0, 4].rowspan == 2
    else:
        assert cells[0, 1].content == "Results" and cells[0, 1].colspan == 4
        assert cells[0, 0].rowspan == rows
    if mode == "three":
        assert [(cells[1, c].content, cells[1, c].colspan) for c in (1, 3)] == [("Front", 2), ("Rear", 2)]
    if mode == "section":
        assert cells[4, 0].content == "Scoped" and cells[4, 0].colspan == 5
    assert [cells[rows, c].content for c in range(1, 5)] == ["-1.50", "+2", "0", "8.00"]
    members = [idx for cell in candidate.cells for idx in cell.source_char_indices]
    assert len(members) == len(set(members))


@pytest.mark.parametrize("mode", ["wrapped", "off-center", "broken-rule", "physical-conflict", "overlap", "loose-single"])
def test_column_group_signal_rejects_unproved_spans(mode):
    """单列折行、偏心标题、不完整下划线、冲突竖线、列缝被穿越和孤立续行均不能证明分组。"""
    table = _grouped_table(mode)
    assert build_column_group_candidate(table, build_native_table_text(table)) is None


@pytest.mark.parametrize("border", ["single", "top", "bottom", "both"])
@pytest.mark.parametrize("font,left,spacing", [("Helvetica", 40, 51), ("Times-Roman", 78, 58)])
def test_public_grouped_table_keeps_semantic_header_and_outside_text(font, left, spacing, border):
    """以公有 PDF 字节入口检验列组、新逻辑行、原生数值与表外题注；字体与实际栏宽独立变化。"""
    out = BytesIO()
    pdf = Canvas(out, pagesize=(490, 480))
    x = [left + spacing * (i + 0.5) for i in range(5)]
    pdf.setFont(font, 10)
    pdf.drawString(left, 445, "Table 2. Production totals.")
    pdf.setLineWidth(0.3)
    outer = [434, 394, 275]
    if border in ("top", "both"):
        outer.append(430.5)
    if border in ("bottom", "both"):
        outer.append(278.5)
    for y in outer:
        pdf.line(left, y, left + 5 * spacing, y)
    pdf.line(x[1] - spacing / 3, 416, x[4] + spacing / 3, 416)
    pdf.drawCentredString(x[0], 409, "Name")
    pdf.drawCentredString((x[1] + x[4]) / 2, 419, "Measurements")
    for pos, val in zip(x[1:], ("North", "East", "South", "West"), strict=True):
        pdf.drawCentredString(pos, 400, val)
    for i in range(6):
        for j, val in enumerate((f"Station {i}", "3.70", "-1.20", "0", "+4.0")):
            pdf.drawCentredString(x[j], 378 - i * 18, val)
    pdf.drawString(left, 256, "The station records were reviewed independently.")
    pdf.showPage()
    pdf.save()
    page = parse(out.getvalue(), file_suffix="pdf", parse_mode="txt").to_dict()["pages"][0]
    tables = [b for b in page["blocks"] if b["type"] == "table"]
    assert len(tables) == 1
    content = next(c["content"] for c in tables[0]["content"] if c["type"] == "table_body")
    dom = BeautifulSoup(content, "html.parser")
    assert len(dom.select("tr")) == 8
    assert dom.select_one('td[colspan="4"]').get_text(strip=True) == "Measurements"
    assert dom.select_one('td[rowspan="2"]').get_text(strip=True) == "Name"
    assert [c.get_text(strip=True) for c in dom.select("tr")[-1].select("td")] == ["Station 5", "3.70", "-1.20", "0", "+4.0"]
    assert "independently" not in content


@pytest.mark.parametrize("position", ["top", "bottom", "middle", "heading-ink"])
def test_double_rules_only_collapse_outside_all_rows(position):
    """同宽的近邻横线位于表外才可当边框；夹住正文或穿过组标题时须保留真实分隔。"""
    from dataclasses import replace

    table = _grouped_table()
    y = {"top": 2.0, "bottom": 139.0, "middle": 66.0, "heading-ink": 10.0}[position]
    additions = [y, y + 2] if position == "middle" else [y]
    rails = tuple(NativeTableRule((0, y, 230, y + 0.2), 0.2, "horizontal") for y in additions)
    modified = replace(table, drawing_lines=(*table.drawing_lines, *rails))
    candidate = build_column_group_candidate(modified, build_native_table_text(modified))
    assert bool(candidate) == (position in {"top", "bottom"})


def test_equal_size_header_refinement_checks_original_body_members():
    """单独更改一个旧数据格的文字或成员，候选均须拒绝等尺寸表头替换。"""
    from dataclasses import replace

    from docvortex.analyzers.native.pdf._table_recovery.column_groups import _preserves_existing_grid

    table = _grouped_table("ruled-odd")
    new = build_column_group_candidate(table, build_native_table_text(table))
    assert new is not None
    parent = next(c for c in new.cells if c.content == "Independent")
    narrower = replace(parent, col=2, colspan=1)
    empty = replace(parent, colspan=1, content="", source_char_indices=())
    old_cells = tuple(c for c in new.cells if c is not parent) + (empty, narrower, replace(empty, col=3))
    old = replace(new, cells=old_cells)
    assert _preserves_existing_grid(new, old, 2, {(1, 4)})
    assert not _preserves_existing_grid(new, old, 2, set())
    value = next(c for c in old.cells if c.row == 3 and c.col == 2)
    for mutation in (replace(value, content="wrong"), replace(value, source_char_indices=value.source_char_indices[::-1])):
        changed = replace(old, cells=tuple(mutation if c is value else c for c in old.cells))
        assert not _preserves_existing_grid(new, changed, 2, {(1, 4)})


@pytest.mark.parametrize(
    "later,accept",
    [
        ("left", True),
        ("further-left-aligned", True),
        ("same-width", False),
        ("new-section", False),
        ("barrier", False),
        ("centered", False),
        ("drift", False),
    ],
)
def test_later_section_width_needs_the_same_unbroken_indent(later, accept):
    """前两项等宽时可使用本节后续异宽项目；禁止越过分节、实体横线、居中变宽或累积漂移。"""
    from types import SimpleNamespace

    from docvortex.analyzers.native.pdf._table_recovery.column_groups import _body_specs

    def row(left, width, y, *, label=False):
        """相同的三列数值记录只变动第一列词框，用完整几何区分居中和左对齐。"""
        cols = [(left, left + width)] if label else [(left, left + width), (86, 95), (142, 153)]
        tokens = [SimpleNamespace(bbox=(a, y, b, y + 5), text="Scope" if label else "5") for a, b in cols]
        return SimpleNamespace(tokens=tokens, bbox=(left, y, cols[-1][1], y + 5))

    rows = [row(4, 18, 4), row(4, 15, 12), row(4, 17, 20, label=True), row(10, 12, 28), row(10, 12, 36)]
    if later == "new-section":
        rows.extend([row(4, 20, 44, label=True), row(10, 28, 52), row(10, 10, 60)])
    elif later == "drift":
        rows.extend([row(11, 12, 44), row(12, 30, 52)])
    elif later == "further-left-aligned":
        rows.extend([row(10, 12, 44), row(10, 27, 52)])
    else:
        left = 3 if later == "centered" else 10
        width = 12 if later == "same-width" else 26
        rows.append(row(left, width, 44))
    rails = [0, 42] if later == "barrier" else [0]
    specs = _body_specs(rows, rails, [0, 65, 125, 175], rows[-1].bbox[3] + 2, 5, 2)
    assert (specs is not None) == accept
    if accept:
        assert [s.col for s in specs if s.row == 4] == [0, 1, 2]
