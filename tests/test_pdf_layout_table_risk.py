from copy import deepcopy

from docvortex.analyzers.native.pdf.layout_table_risk import table_structure_risk


def _table(content, bbox=(0.08, 0.15, 0.91, 0.44)):
    """构造与 Flash 布局诊断同阶段的归一化表格块。"""
    return {"type": "table", "bbox": list(bbox), "content": content}


def test_parallel_numbered_figures_in_empty_table_rows():
    """同一表首行中的两个图题以及空白图面和来源行共同构成错判证据。"""
    table = _table(
        "<table><tbody>"
        "<tr><th>Fig. 8: Input signal</th><th>图 9：重建信号</th></tr>"
        "<tr><td></td><td>&nbsp;</td></tr>"
        "<tr><td>Source: laboratory records</td><td>数据来源：本次实验</td></tr>"
        "</tbody></table>"
    )
    blocks = [table]
    before = deepcopy(blocks)
    risk = table_structure_risk(blocks)
    assert risk is not None and risk[0] == 60
    assert "2 个编号图题" in risk[1]
    assert "[0.080, 0.150, 0.910, 0.440]" in risk[1]
    assert blocks == before

    table["content"] = table["content"].replace("<td></td>", '<td><img src="signal.png"></td>')
    assert table_structure_risk(blocks) is None
    table["content"] = before[0]["content"].replace("图 9：重建信号", "Fig. 8: another mention")
    assert table_structure_risk(blocks) is None
    table["content"] = before[0]["content"].replace("Fig. 8: Input signal", "Fig. 8 shows behavior")
    assert table_structure_risk(blocks) is None


def test_table_projection_contains_prose_before_its_own_caption():
    """题前是独立段落且题后有重复三列时，标出跨越真实表起始边界的框。"""
    table = _table(
        "relative value = (1 + 2) / 100\n"
        "total = 3\n"
        "(1) Stability\n"
        "(2) Repeatability\n"
        "The equipment was measured and calibrated at the start.\n"
        "The coefficients were recorded. The results can be compared across labs.\n"
        "        Table 7: Measurement comparison\n"
        "Unit         Standard           Result\n"
        "Seconds      Control setting    0.27\n"
        "Coulombs     Positive terminal  1.50"
    )
    risk = table_structure_risk([table])
    assert risk is not None and risk[0] == 60
    assert "2 行长正文" in risk[1] and "3 行多列数据" in risk[1]

    table["content"] = table["content"].replace("Table 7: Measurement comparison", "Table 7 shows the measurements")
    assert table_structure_risk([table]) is None


def test_real_single_column_and_long_technical_tables():
    """单栏表尤其允许长句和内部引用，多列技术表允许长说明单元格和分组表头。"""
    single_column = _table(
        "The equipment was measured and calibrated at the start.\n"
        "The coefficients were recorded. The results can be compared across labs.\n"
        "Examples of settings to compare\nOther allowed descriptions\n"
        "Requires two independent measurements\n"
        "Table 6: Measurement comparison\n"
        "Verify the source signal against the approved documentation.\n"
        "Collect a second measurement with the same procedure.\n"
        "Retain the results for further analysis."
    )
    statements = _table(
        "<table><tr><th>Item</th><th>2026</th><th>2025</th></tr>"
        "<tr><td>Measure and record the final impairment amount. Refer to Table 6: Measurement comparison.</td>"
        "<td>4,589,029</td><td>4,432,138</td></tr>"
        "<tr><td>Payment for equipment with limits identified in the corresponding provision.</td>"
        "<td>4,602,103</td><td>4,102,677</td></tr>"
        "<tr><td>Total</td><td>9,191,132</td><td>8,534,815</td></tr></table>"
    )
    assert table_structure_risk([single_column, statements]) is None


def test_header_only_table_with_a_row_of_external_data():
    """原生表仅含列名且其下是独立对齐的数据列时按整页回退，刻度和通栏正文不能充当数据。"""
    header = {
        "type": "table",
        "bbox": [0.1, 0.20, 0.9, 0.24],
        "content": "<table><tbody><tr><td>Item</td><td>Code</td><td>Name</td><td>Description</td></tr></tbody></table>",
    }
    row = [
        {"type": "text", "bbox": [x, 0.245, right, 0.268], "content": s}
        for x, right, s in ((0.11, 0.16, "low"), (0.22, 0.32, "V+19"), (0.41, 0.58, "Open"), (0.64, 0.86, "An open port"))
    ]
    assert table_structure_risk([header, *row])[0] == 30
    assert table_structure_risk([header, *[{**b, "content": str(i)} for i, b in enumerate(row)]]) is None
    assert (
        table_structure_risk([header, {"type": "text", "bbox": [0.11, 0.25, 0.88, 0.28], "content": "Independent paragraph."}])
        is None
    )


def test_separate_numbered_table_inside_projected_data():
    """窄栏的两组多列数值被题注隔开时应接管，只含数据词或正文引用时保持原生。"""
    for title in ("Table II: Validation results", "表 2：独立测量结果"):
        rows = ["Alpha   10   20", "Beta   15   25", title, "Name   Cold   Hot", "Gamma   30   40", "Delta   34   44"]
        table = _table("\n".join(rows), bbox=(0.53, 0.14, 0.91, 0.33))
        risk = table_structure_risk([table])
        assert risk and risk[0] == 30 and "表体内的编号表题" in risk[1]
        for ordinary in ("table         0.656     0.61", "Table 2 shows the results", "Reading from Table 2"):
            assert table_structure_risk([{**table, "content": table["content"].replace(title, ordinary)}]) is None
        assert table_structure_risk([{**table, "content": "\n".join(rows[1:])}]) is None


def test_truncated_numeric_table_with_aligned_external_rows():
    """原页至少两列跨三条基线继续，某一年的值缺失仍能证明表截断，注释和无数据表头不能充当上表。"""
    table = _table(
        "<table><tr><th>Period</th><th>2022</th><th>2023</th><th>2024</th></tr>"
        "<tr><td>Receipts</td><td>152</td><td>249</td><td>320</td></tr>"
        "<tr><td>Interest</td><td>5</td><td>27</td><td>38</td></tr></table>",
        bbox=(0.1, 0.15, 0.90, 0.35),
    )
    cells = [
        {"type": "text", "bbox": [x, y, x + w, y + 0.014], "content": text}
        for index, y in enumerate((0.362, 0.398, 0.434))
        for x, w, text in (
            (0.11, 0.19, f"Operating category {index}"),
            (0.50, 0.045, str(215 + index)),
            (0.64, 0.045, str(319 + index)),
            (0.78, 0.045, f"( {411 + index} )" if index else "—"),
        )
    ]
    risk = table_structure_risk([table, *reversed(cells)])
    assert risk and risk[0] == 30 and "表外 3 行" in risk[1]
    barrier = {"type": "caption", "bbox": [0.1, 0.351, 0.88, 0.358], "content": "Table 2: Independent series"}
    assert table_structure_risk([table, *cells, barrier]) is None
    assert table_structure_risk([table, *cells[:8]]) is None
    damaged = _table(
        "<table><tr><th>Region</th><th>Fees</th></tr><tr><td>Category</td><td>Unit</td></tr>"
        "<tr><td>Source fees</td><td>7,9731,844,61</td></tr></table>",
        bbox=tuple(table["bbox"]),
    )
    assert table_structure_risk([damaged, *cells]) is None
    one_value_row = table["content"].replace("<tr><td>Interest</td><td>5</td><td>27</td><td>38</td></tr>", "")
    one_value_row = one_value_row.replace("<table>", "<table><tr><th>Reported year</th><th>2020</th><th>2021</th></tr>")
    assert table_structure_risk([{**table, "content": one_value_row}, *cells]) is None
