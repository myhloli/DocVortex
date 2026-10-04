"""用独立原页事实验证原生 benchmark 修复，避免按候选结果更新真值。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from docvortex.analyzers.native.pdf.pipeline import _analyze_native_document
from docvortex.document.pdf import PDFDocument
from docvortex.content.spans import inline_span_plain_text

ROOT = Path(__file__).parents[2]
MANIFEST = json.loads((ROOT / "tests/fixtures/benchmark_native_manifest.json").read_text())


def _model(name: str) -> list[dict]:
    """校验原件身份后解析全部页，直接验证原生成员与结构。"""
    record = next(item for item in MANIFEST["documents"] if item["id"] == name)
    source = (ROOT / record["path"]).read_bytes()
    assert hashlib.sha256(source).hexdigest() == record["sha256"]
    with PDFDocument(source) as document:
        pages = _analyze_native_document(document)
    assert len(pages) == 1
    return [
        {
            **block,
            "content": inline_span_plain_text(block["content"]) if isinstance(block["content"], list) else block["content"],
        }
        for block in pages[0]
    ]


@pytest.mark.parametrize(
    "name,shapes",
    [
        ("portfolio_sparse_table", [(6, 3)]),
        ("linked_initiatives_table", [(4, 4)]),
        ("competence_filled_table", [(6, 2)]),
        ("mineral_units_table", [(7, 2)]),
        ("erosion_multilevel_tables", [(8, 6), (9, 5)]),
        ("training_grouped_table", [(6, 7)]),
    ],
)
def test_real_native_table_topology(name: str, shapes: list[tuple[int, int]]) -> None:
    """真实表格必须输出单元格结构，不能以空间投影或错误细分代替。"""
    tables = [block for block in _model(name) if block["type"] == "table"]
    assert len(tables) == len(shapes)
    for block, (rows, cols) in zip(tables, shapes):
        assert block["content"].startswith("<table")
        soup = BeautifulSoup(block["content"], "html.parser")
        assert len(soup.find_all("tr")) == rows
        occupied = set()
        for row, tr in enumerate(soup.find_all("tr")):
            col = 0
            for cell in tr.find_all(["td", "th"], recursive=False):
                while (row, col) in occupied:
                    col += 1
                for r in range(row, row + int(cell.get("rowspan", 1))):
                    for c in range(col, col + int(cell.get("colspan", 1))):
                        assert (r, c) not in occupied
                        occupied.add((r, c))
                col += int(cell.get("colspan", 1))
        assert occupied == {(r, c) for r in range(rows) for c in range(cols)}
        if name == "competence_filled_table":
            heading = next(cell for cell in soup.find_all("td") if "Learning Outcomes" in cell.get_text())
            assert heading.get("colspan") == "2"
        if name == "training_grouped_table":
            spans = {
                cell.get_text(): (int(cell.get("rowspan", 1)), int(cell.get("colspan", 1))) for cell in soup.find_all("td")
            }
            assert spans["Properties"] == (3, 1)
            assert spans["Training Datasets"] == (1, 6)
            assert spans["Instruction"] == spans["Alignment"] == (1, 3)


@pytest.mark.parametrize(
    "name,count",
    [
        ("dotted_contents", 1),
        ("replication_sidebar", 4),
        ("recommendation_panels", 4),
        ("search_value_panels", 7),
        ("ocr_performance_panels", 3),
    ],
)
def test_real_heading_boundaries(name: str, count: int) -> None:
    """真实标题数量来自原页层次，同时验证简介未被额外提升为标题。"""
    blocks = _model(name)
    titles = [block for block in blocks if block["type"] in {"doc_title", "paragraph_title"}]
    assert len(titles) == count
    if name == "dotted_contents":
        assert titles[0]["content"] == "Contents"
        index = next(block for block in blocks if block["type"] == "index")
        assert len(index["content"].splitlines()) == 24
        assert "21. Integration" in index["content"]
    if name == "recommendation_panels":
        assert "Recommendation model Hit Ratio comparison" not in [block["content"] for block in titles]


def test_real_step_children_stay_before_next_parent() -> None:
    """原页的第一步骤及六个子步骤必须先于第二步骤，不能被全局切栏拆开。"""
    blocks = _model("microscope_steps")
    contents = [block["content"] for block in blocks]
    first = next(i for i, value in enumerate(contents) if value.startswith("1. When changing"))
    last_child = next(i for i, value in enumerate(contents) if value.startswith("f. The depth"))
    following = next(i for i, value in enumerate(contents) if value.startswith("2. When changing"))
    assert first < last_child < following


def test_real_panel_body_follows_its_heading() -> None:
    """三栏说明必须依次完整输出，不应先扫过三栏标题再扫正文。"""
    blocks = _model("search_value_panels")
    content = "\n".join(block["content"] for block in blocks)
    assert (
        content.index("Higher Return of Information")
        < content.index("Unlike existing search systems")
        < content.index("Optimal Attempt")
    )
    assert (
        content.index("Reduced Information Acquisition Time")
        < content.index("By returning all semantic")
        < content.index("SOTA")
    )


def test_real_rotated_axis_labels_follow_page_position() -> None:
    """日期顺序由横轴几何决定，不能因旋转字框细微高差把后一年的标签提前。"""
    image = next(block for block in _model("migration_charts") if block["type"] == "image")
    assert image["content"].index("01/2019") < image["content"].index("01/2020") < image["content"].index("01/2021")


def test_real_chart_dates_and_legend_follow_data() -> None:
    """原页三根柱的日期共享横轴行，底部图例必须在日期之后，不能由对象顺序提前。"""
    image = next(block for block in _model("employment_charts") if block["type"] == "image")
    dates = ["July 2020", "October 2020", "January 2021"]
    assert any(all(date in row for date in dates) for row in image["content"].splitlines())
    assert image["content"].index("January 2021") < image["content"].index("Will not terminate employment")
