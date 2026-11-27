"""少线表正文行、跨列表题、跨层表头和歧义正文的守恒断言。"""

from io import BytesIO

import pytest
from bs4 import BeautifulSoup
from reportlab.pdfgen.canvas import Canvas

from docvortex import parse
from docvortex.analyzers.native.pdf._table_recovery.body_tracks import build_body_track_candidate
from docvortex.analyzers.native.pdf._table_recovery.contracts import NativeTableInput, NativeTableRule
from docvortex.analyzers.native.pdf._table_recovery.text import build_native_table_text
from test_native_pdf_table import _char_items


def _sparse_table(shape="simple", dx=0, dy=0, width=260):
    """构造源坐标不同、词形与正文内容有变化的表格；物理横隔与文字证据可独立移除。"""
    coords = (20, width * 0.37, width * 0.63, width - 10)
    entries = []

    def line(y, values):
        """按指定的源字宽与相对栏位，保留实际空格和负号。"""
        for x, value in zip(coords, values, strict=True):
            if value:
                half = len(value) * 2.05
                entries.append((value, (dx + x - half, dy + y, dx + x + half, dy + y + 7)))

    line(4, ("Code", "Element", "Year", "Count"))
    if shape == "centered":
        line(23, ("", "Model AB", "", ""))
        line(28, ("A1", "", "2014", "-15"))
        line(33, ("", "X", "", ""))
    elif shape == "ambiguous":
        line(23, ("A1", "Model", "2014", "-15"))
        line(34.5, ("", "X", "", ""))
    else:
        line(23, ("A1", "Model", "2014", "-15"))
    for y, ids in [(46, ("B2", "Beta", "", "0")), (66, ("C3", "Gamma", "2018", "+3")), (86, ("D4", "Pi", "2020", "1.20"))]:
        line(y, ids)
    rails = [0, 16, 100]
    if shape == "no-header":
        rails.remove(16)
    if shape == "crossing-border":
        rails.insert(2, 26)
    drawing = tuple(NativeTableRule((dx, dy + y, dx + width, dy + y + 0.25), 0.25, "horizontal") for y in rails)
    return NativeTableInput((dx, dy, dx + width, dy + 100.3), (dx + width + 80, dy + 250), 0, _char_items(entries), drawing)


@pytest.mark.parametrize("dx,dy,width", [(0, 0, 260), (47, 63, 315), (72, 9, 230)])
def test_centered_wrapped_value_uses_the_multi_column_body_row(dx, dy, width):
    """居中换行不派生两张伪行；原有空值、符号、尾零和唯一归属均需保留。"""
    table = _sparse_table("centered", dx, dy, width)
    candidate = build_body_track_candidate(table, build_native_table_text(table))
    assert candidate and (candidate.rows, candidate.cols) == (5, 4)
    actual = {(c.row, c.col): c.content for c in candidate.cells}
    assert actual[1, 0] == "A1" and actual[1, 1].replace(" ", "") == "ModelABX"
    assert actual[1, 3] == "-15" and actual[2, 2] == "" and actual[3, 3] == "+3" and actual[4, 3] == "1.20"
    members = [i for c in candidate.cells for i in c.source_char_indices]
    assert len(members) == len(set(members))


@pytest.mark.parametrize("shape", ["no-header", "ambiguous", "crossing-border"])
def test_body_candidate_preserves_unproven_row_boundaries(shape):
    """无表头隔线、两侧等距的单栏文字、横穿正文的边界均不能强制解释为折行。"""
    table = _sparse_table(shape)
    assert build_body_track_candidate(table, build_native_table_text(table)) is None


@pytest.mark.parametrize("shift,font", [(0, "Helvetica"), (26, "Times-Roman")])
def test_pdf_keeps_unruled_body_in_separate_cells(shift, font):
    """正常公开入口须保住表头换行、逐行数值、空格及表外说明，换字体后仍采用实体分列。"""
    buf = BytesIO()
    pdf = Canvas(buf, pagesize=(460, 430))
    left, right = 45 + shift, 355 + shift
    for y in (360, 324, 190):
        pdf.line(left, y, right, y)
    for x in (left, left + 90, left + 200, right):
        pdf.line(x, 190, x, 360)
    pdf.setFont(font, 10)
    pdf.drawString(left, 376, "Table 3. Independent field records")
    for x, txt in zip((left + 42, left + 145, left + 255), ("Element", "Reading", "Result")):
        pdf.drawCentredString(x, 341, txt)
    pdf.drawCentredString(left + 145, 330, "Range (kPa)")
    pdf.drawCentredString(left + 255, 330, "Amount")
    vals = [
        ("Alder", "<4.7", "-1.20"),
        ("", "4.7 to 5", "0"),
        ("Birch", "Any range", "7.5"),
        ("Cedar", "<2", "0"),
        ("", ">2", "+8.30"),
        ("Elm", "Any range", "+4.25"),
        ("Pine", "Any range", "12"),
    ]
    for k, row in enumerate(vals):
        for x, txt in zip((left + 42, left + 145, left + 255), row):
            pdf.drawCentredString(x, 311 - 17 * k, txt)
    pdf.drawString(left, 162, "These measurements were checked independently.")
    pdf.showPage()
    pdf.save()
    page = parse(buf.getvalue(), file_suffix="pdf", parse_mode="txt").to_dict()["pages"][0]
    tables = [b for b in page["blocks"] if b["type"] == "table"]
    assert len(tables) == 1
    html = next(c["content"] for c in tables[0]["content"] if c["type"] == "table_body")
    rows = [[td.get_text(strip=True) for td in tr.select("td")] for tr in BeautifulSoup(html, "html.parser").select("tr")]
    assert rows == [["Element", "Reading Range (kPa)", "Result Amount"], *[list(row) for row in vals]]
    assert "independently" not in html


@pytest.mark.parametrize("mode", ["grouped", "missing-rule", "missing-leaf", "misaligned-rule"])
def test_partial_header_tracks_require_both_group_labels_and_each_leaf(mode):
    """只在实体竖轨及叶子表头共同支持时确认跨列，半截竖线不能伪造表头层级。"""
    coords = (20, 90, 145, 207, 265)
    entries = []

    def entry(x, y, word):
        """以文字自身中心构造可独立缩放的字符框。"""
        n = len(word) * 2
        entries.append((word, (x - n, y, x + n, y + 6)))

    entry(20, 11, "Name")
    entry(113, 3, "Group A")
    entry(236, 3, "Group B")
    for j, name in enumerate(("Low", "High", "Early", "Late"), 1):
        if mode != "missing-leaf" or j != 2:
            entry(coords[j], 19, name)
    for i, name in enumerate(("red", "green", "blue", "pink")):
        for x, val in zip(coords, (name, "+4", "3.0", "-6", "0"), strict=True):
            entry(x, 43 + 17 * i, val)
    rules = [NativeTableRule((0, y, 300, y + 0.2), 0.2, "horizontal") for y in (0, 34, 117)]
    for x in (55, 176):
        yy = 7 if mode == "missing-rule" else 0
        ox = 13 if mode == "misaligned-rule" else 0
        rules.append(NativeTableRule((x + ox, yy, x + ox + 0.2, 29), 0.2, "vertical"))
    t = NativeTableInput((0, 0, 300, 117.2), (300, 200), 0, _char_items(entries), tuple(rules))
    c = build_body_track_candidate(t, build_native_table_text(t))
    if mode != "grouped":
        assert c is None
        return
    assert c is not None and (c.rows, c.cols) == (6, 5)
    assert [(q.content, q.rowspan, q.colspan) for q in c.cells if q.row == 0] == [
        ("Name", 2, 1),
        ("Group A", 1, 2),
        ("Group B", 1, 2),
    ]
    assert sum(q.content == "-6" for q in c.cells) == 4


@pytest.mark.parametrize("mode", ["parallel", "parallel-cjk", "labels", "numeric", "independent-sentences"])
def test_parallel_wrapped_paragraphs_do_not_make_data_rows(mode):
    """跨列文字各自只在末行结句时保持原表示；真正的数值、短标签和独立句仍可分行。"""
    from types import SimpleNamespace

    from docvortex.analyzers.native.pdf._table_recovery.body_tracks import _parallel_wrapped_prose

    left = ["A term which starts", "and continues for", "another printed line."]
    right = ["A different phrase", "continues here on", "its final printed line."]
    if mode == "parallel-cjk":
        left = ["首栏正文自这里起", "接着该栏继续到", "下面这个位置结束。"]
        right = ["旁边一栏也有正文", "另一句一直延伸到", "本段的最后位置。"]
    if mode == "labels":
        left, right = ["A", "B", "C"], ["Pi", "Mu", "Nu"]
    if mode == "numeric":
        right = ["-2.50", "+4.20", "7.10"]
    if mode == "independent-sentences":
        left[1] += "."
    slots = [
        {j: SimpleNamespace(text=v, bbox=(j * 100, i * 10, j * 100 + 4 * len(v), i * 10 + 6)) for j, v in enumerate(pair)}
        for i, pair in enumerate(zip(left, right))
    ]
    assert _parallel_wrapped_prose(slots, 2, 6) == (mode in {"parallel", "parallel-cjk"})


@pytest.mark.parametrize("separated", [True, False])
def test_partial_horizontal_header_rule_preserves_wrapped_unit(separated):
    """同一几何和文字只改变横线左端，区分跨两行的带单位标签与真正的两级标题。"""
    from types import SimpleNamespace

    from docvortex.analyzers.native.pdf._table_recovery.body_tracks import _grouped_header_cells
    from docvortex.analyzers.native.pdf._table_recovery.sparse_common import _LocalRule

    def head(y, cells):
        """保留平行原生表头字带，使分组方向完全由实线而非词串决定。"""
        return SimpleNamespace(
            bbox=(0, y, 150, y + 5), tokens=[SimpleNamespace(text=t, bbox=(a, y, b, y + 5)) for t, a, b in cells]
        )

    lines = [
        head(3, [("Flow", 10, 40), ("Production", 84, 125)]),
        head(17, [("units", 10, 35), ("Day", 68, 85), ("Night", 120, 144)]),
    ]
    tracks = (0, 55, 100, 155)
    rules = (_LocalRule("vertical", 55, 2, 24, 0.3), _LocalRule("horizontal", 12, 0 if separated else 55, 155, 0.3))
    cells = _grouped_header_cells(lines, tracks, rules, 26, 8)
    assert cells is not None
    assert next(c for c in cells if c.row == 0 and c.col == 0).rowspan == (1 if separated else 2)
    assert next(c for c in cells if c.row == 0 and c.col == 1).colspan == 2
