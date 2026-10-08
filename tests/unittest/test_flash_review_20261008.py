"""以两份完整原件及独立原页断言验收十月八日 Flash 布局问题。"""

from functools import lru_cache
from pathlib import Path
import re
import json
import unicodedata

from bs4 import BeautifulSoup
import pytest

from _flash_pdf_test_utils import _visible_text
from docvortex import parse

FIXTURES = Path(__file__).parent / "pdfs/flash_review_20261008"


@lru_cache(maxsize=2)
def _result(name):
    """完整解析原件，保留页眉、标题和跨页表格所需上下文。"""
    return parse(FIXTURES / f"{name}.pdf", keep_model_json=True)


def _blocks(name, page):
    """返回最终物理页块，不把历史显示编号当作列表下标。"""
    return _result(name).to_dict()["pages"][page - 1].get("blocks", [])


def _leaves(blocks):
    """遍历视觉父块内的叶子，文字内联 span 保持在所属正文中。"""
    for block in blocks:
        children = block.get("content")
        if isinstance(children, list) and any(isinstance(child, dict) and "bbox" in child for child in children):
            yield from _leaves(children)
        else:
            yield block


def _text(block):
    """只归一化空白，保留原页金额、标点和数学符号。"""
    return re.sub(r"\s+", "", _visible_text(block.get("content", "")))


def _tables(name, page):
    """读取真实 HTML 单元格，拒绝以纯文本投影冒充结构恢复。"""
    return [
        BeautifulSoup(block["content"], "html.parser").find("table")
        for block in _leaves(_blocks(name, page))
        if block["type"] == "table_body"
    ]


def _cell_text(value):
    """允许表格既有的括号宽窄归一化，金额、文字及其他标点仍逐字符核对。"""
    return unicodedata.normalize("NFKC", re.sub(r"\s+", "", value)).replace("—", "-")


def _matrix(table):
    """按显式合并单元格展开网格，便于核对原页金额与表头的逻辑列归属。"""
    cells = {}
    for r, row in enumerate(table.find_all("tr")):
        col = 0
        for cell in row.find_all(["td", "th"], recursive=False):
            while (r, col) in cells:
                col += 1
            rowspan, colspan = int(cell.get("rowspan", 1)), int(cell.get("colspan", 1))
            for dr in range(rowspan):
                for dc in range(colspan):
                    cells[r + dr, col + dc] = _cell_text(cell.get_text())
            col += colspan
    width = max(c for r, c in cells) + 1
    return [[cells.get((r, c), "") for c in range(width)] for r in range(max(r for r, c in cells) + 1)]


@pytest.mark.parametrize("page", range(14, 22))
def test_financial_statement_all_independently_transcribed_cells(page):
    """原页独立转写的全部数据及空值小节逐行核对，不能仅用关键汇总行验收。"""
    truth = json.loads((Path(__file__).parents[1] / "fixtures/flash_review_20261008_cells.json").read_text())
    expected = [[_cell_text(row["label"]), *row["values"]] for row in truth["pages"][str(page)]]
    matrix = _matrix(_tables("quarterly_report", page)[0])
    assert matrix[4:] == expected
    assert matrix[0] == ["", "本集团", "本集团", "本行", "本行"]
    assert matrix[1] == ["", "2026年", "2025年", "2026年", "2025年"]
    date = ["3月31日", "12月31日"] * 2 if page <= 16 else ["1-3月"] * 4
    assert matrix[2] == ["", *date]
    assert matrix[3] == ["", *(["未经审计", "经审计"] * 2 if page <= 16 else ["未经审计"] * 4)]


@pytest.mark.parametrize("key", ["3-0", "4-1", "12-0", "12-1", "12-2"])
def test_metrics_capital_and_leverage_all_cells(key):
    """指标、原因、资本和杠杆表按原页逐格核对，包含表头和跨行说明。"""
    truth = json.loads((Path(__file__).parents[1] / "fixtures/flash_review_20261008_cells.json").read_text())
    page, table_index = map(int, key.split("-"))
    matrix = _matrix(_tables("quarterly_report", page)[table_index])
    # 日期字段在杠杆表占两条物理行；逻辑表头按同一列拼接后再核对。
    if key == "12-1":
        matrix = [[matrix[1][0], *[matrix[0][c] + matrix[1][c] for c in range(1, 5)]], *matrix[2:]]
    assert matrix == [[_cell_text(c) for c in row] for row in truth["other_tables"][key]]


@pytest.mark.parametrize("page", range(14, 22))
def test_financial_statement_keeps_complete_five_column_grid(page):
    """财务报表含项目和四个金额列，空值、续页及完整汇总行不能退为目录或散文。"""
    tables = _tables("quarterly_report", page)
    assert len(tables) == 1 and tables[0] is not None
    rows = tables[0].find_all("tr")
    assert max(sum(int(cell.get("colspan", 1)) for cell in row.find_all(["td", "th"], recursive=False)) for row in rows) == 5
    assert not any(block["type"] == "index" for block in _blocks("quarterly_report", page))
    # 原页关键完整行逐项抄录，金额四列不能与项目列分开认领。
    expected = {
        14: ["现金及存放中央银行款项", "273,649", "254,754", "270,786", "251,530"],
        15: ["负债合计", "7,133,945", "7,129,370", "6,931,351", "6,918,889"],
        16: ["负债和股东权益总计", "7,847,897", "7,832,567", "7,617,205", "7,593,974"],
        17: ["五、净利润", "11,491", "12,782", "11,281", "12,481"],
        18: ["七、综合收益总额", "10,801", "9,376", "10,748", "9,102"],
        19: ["经营活动产生的现金流量净额", "(357)", "(117,455)", "1,637", "(93,761)"],
        20: ["筹资活动产生的现金流量净额", "(122,891)", "8,063", "(123,351)", "8,144"],
        21: ["六、期末现金及现金等价物余额", "153,296", "187,636", "128,581", "173,842"],
    }[page]
    assert any(
        [_cell_text(cell.get_text()) for cell in row.find_all(["td", "th"], recursive=False)]
        == [_cell_text(value) for value in expected]
        for row in rows
    )


def test_capital_and_leverage_tables_keep_intervening_prose():
    """两张原生表格独立成表，中间杠杆率说明仍为可复制正文。"""
    blocks = _blocks("quarterly_report", 12)
    tables = _tables("quarterly_report", 12)
    assert len(tables) == 3 and all(table is not None for table in tables)
    assert not any(block["type"] == "image" for block in blocks)
    assert any(block["type"] == "text" and "本集团杠杆率为7.63%" in _text(block) for block in blocks)


def test_complete_table_claim_does_not_change_signatory_text_role():
    """完整表体移除后签署矩阵保持正文，不能误晋升标题或降到页脚注。"""
    blocks = _blocks("quarterly_report", 16)
    for anchor in ["高迎欣", "王晓永", "李彬", "张兰波", "会计机构负责人"]:
        assert any(block["type"] == "text" and anchor in _text(block) for block in blocks)


@pytest.mark.parametrize(
    "page,cols,label,values",
    [
        (3, 4, "营业收入", ["37,822", "36,813", "2.74"]),
        (4, 5, "经营活动产生的现金流量净额（人民币百万元）", ["-357", "-117,455", "两期为负"]),
    ],
)
def test_sparse_table_cells_do_not_collapse(page, cols, label, values):
    """四列指标表和五列原因表按原页保留每个指标行及独立数值列。"""
    table = _tables("quarterly_report", page)[-1]
    assert table is not None
    rows = [[_cell_text(c.get_text()) for c in row.find_all(["td", "th"], recursive=False)] for row in table.find_all("tr")]
    assert max(map(len, rows)) == cols
    assert any(row[: 1 + len(values)] == [_cell_text(label), *values] for row in rows)
    if page == 4:
        assert any(row[:3] == [_cell_text("每股经营活动产生的现金流量净额（人民币元）"), "-0.01", "-2.68"] for row in rows)


def test_standard_prose_is_not_code_and_table_caption_is_unique():
    """两章节各保留标题和正文，表二标题唯一归属下方表格。"""
    blocks = _blocks("standard", 15)
    assert not any(block["type"] == "code" for block in blocks)
    for anchor in ["5.12.3耐机械应力", "5.12.4部件力学环境"]:
        assert any(block["type"] == "paragraph_title" and _text(block) == anchor for block in blocks)
    captions = [b for b in _leaves(blocks) if "表2部件的力学环境要求" in _text(b)]
    assert len(captions) == 1 and captions[0]["type"] == "table_caption"


def test_standard_term_number_is_body_and_terms_are_separate():
    """编号三点一不能进入页眉，每个双语术语标题与其定义保持独立边界。"""
    blocks = _blocks("standard", 10)
    assert all("3.1" not in _text(b) for b in blocks if b["type"] == "header")
    for number, anchor in [
        ("3.1", "等效原子序数"),
        ("3.9", "X射线安全检查设备"),
        ("3.10", "微剂量X射线安全检查设备"),
        ("3.11", "透射式微剂量X射线安全检查设备"),
    ]:
        assert any(b["type"] == "paragraph_title" and _text(b).startswith(number + anchor) for b in blocks)
    assert not any(b["type"] == "paragraph_title" and ("来源" in _text(b) or _text(b).startswith("H")) for b in blocks)


def test_all_term_entries_keep_source_notes_outside_titles():
    """全部二十二个条目保留编号边界，来源说明不能在晚期标题分类中再次晋升。"""
    terms = []
    for page in [10, 11, 12]:
        for block in _blocks("standard", page):
            if block["type"] in {"paragraph_title", "doc_title"}:
                assert "来源" not in _text(block)
                match = re.match(r"3\.(\d+)\D", _text(block))
                if match:
                    terms.append(int(match[1]))
    assert terms == list(range(1, 23))


@pytest.mark.parametrize(
    "name,page,anchor", [("standard", 20, "按GB/T2423.1"), ("quarterly_report", 7, "负债总额71,339.45亿元")]
)
def test_body_sentence_is_continuous_without_fake_document_title(name, page, anchor):
    """正常两行句子和经营分析段不能在词中拆成文档标题。"""
    blocks = _blocks(name, page)
    assert not any(b["type"] == "doc_title" for b in blocks)
    assert any(b["type"] == "text" and anchor in _text(b) for b in blocks)
    if name == "standard":
        assert any("规定的试验方法进行，判断结果" in _text(b) for b in blocks if b["type"] == "text")


@pytest.mark.parametrize(
    "page,lead,section", [(9, "微剂量X射线安全检查设备", "1范围"), (24, "附录A", "A.1试验要求"), (25, "附录B", "B.1概述")]
)
def test_leading_title_precedes_first_section(page, lead, section):
    """同栏或跨栏页首标题先于其下章节，保留多栏排序而不全页纵向重排。"""
    blocks = _blocks("standard", page)
    first = next(i for i, b in enumerate(blocks) if lead in _text(b))
    second = next(i for i, b in enumerate(blocks) if section in _text(b))
    assert first < second


def test_appendix_formula_has_complete_public_crop_and_ordered_label():
    """公式主体公开图像非空，点号上下标保留且编号在主体之后出现一次。"""
    blocks = _blocks("standard", 24)
    equations = [(i, b) for i, b in enumerate(blocks) if b["type"] == "equation"]
    assert len(equations) == 1
    index, equation = equations[0]
    assert equation.get("image_path") and _result("standard").assets[equation["image_path"]]
    labels = [i for i, b in enumerate(blocks) if b["type"] == "text" and re.fullmatch(r"[.…]*（A\.1）", _text(b))]
    assert len(labels) == 1 and index < labels[0]
    assert len([b for b in _blocks("standard", 25) if b["type"] == "equation"]) == 1
