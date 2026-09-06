"""真实 Office 样例的原生解析、后处理与素材导出回归。"""

from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup
from docvortex.api import analyze
from docvortex.analyzers.native import RtfModel
from docvortex.export.middle import export_middle_json
from docvortex.postprocess.document import model_json_to_middle_json
from docvortex.schema import BlockType, ChartBlock, ImageBlock, MiddleJson, ModelJson, TableBlock
from _span_test_utils import inline_text, visible_content

_OFFICE_SAMPLE_DIR = Path(__file__).parents[2] / "demo" / "office_docs"


def _analyze_sample(file_suffix: str) -> tuple[MiddleJson, ModelJson]:
    """从公开分析结果构建未导出素材的语义树，以验证 sidecar 导出闭包。"""
    basename = {"doc": "docx", "ppt": "pptx", "xls": "xlsx"}[file_suffix]
    model = analyze(_OFFICE_SAMPLE_DIR / f"{basename}_01.{file_suffix}").model_json
    return model_json_to_middle_json(model), model


def test_real_doc_recovers_sections_structure_and_sidecars(tmp_path: Path) -> None:
    """验证真实 DOC 的 section、目录、表格、图片和严格 export 闭包。"""

    middle, model = _analyze_sample("doc")
    counts = Counter(block.get("type") for page in model.pages for block in page)

    assert len(model.pages) == len(middle.pages) == 3
    assert counts[BlockType.DOC_TITLE] == 1
    assert counts[BlockType.PARAGRAPH_TITLE] == 37
    assert counts[BlockType.INDEX] == 1
    assert counts[BlockType.LIST] == 5
    assert counts[BlockType.TABLE] == 8
    assert counts[BlockType.HEADER] == 4
    assert counts[BlockType.FOOTER] == 1
    assert counts[BlockType.IMAGE] >= 44
    assert counts[BlockType.CHART] == 1

    table_blocks = [block for page in middle.pages for block in page.blocks if isinstance(block, TableBlock)]
    assert len(table_blocks) == 8
    soups = [BeautifulSoup(block.content[0].content, "html.parser") for block in table_blocks]
    assert sum(max(len(soup.find_all("table")) - 1, 0) for soup in soups) == 3
    assert sum(len(soup.find_all("img")) for soup in soups) == 5
    assert any(len(soup.find_all("tr")) == 39 for soup in soups)
    assert any(
        len([cell for cell in soup.find_all(["td", "th"]) if cell.has_attr("rowspan") or cell.has_attr("colspan")]) == 141
        for soup in soups
    )
    chart = next(block for page in middle.pages for block in page.blocks if isinstance(block, ChartBlock))
    chart_soup = BeautifulSoup(chart.content[0].content, "html.parser")
    assert [
        [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"], recursive=False)]
        for row in chart_soup.find_all("tr")
    ] == [
        ["列1", "系列 1", "系列 2", "系列 3"],
        ["类别 1", "4.3", "2.4", "2"],
        ["类别 2", "2.5", "4.4", "2"],
        ["类别 3", "3.5", "1.8", "3"],
        ["类别 4", "4.5", "2.8", "5"],
    ]
    assert chart.content[0].image_base64 is not None

    export = export_middle_json(middle, tmp_path / "export")
    payload = export.middle_json.model_dump_json(exclude_none=True)
    assert export.json_path.exists()
    assert len(export.image_paths) >= 49
    assert all(path.exists() and path.stat().st_size > 0 for path in export.image_paths)
    assert "image_base64" not in payload
    assert "data:image/" not in payload


def test_real_ppt_recovers_table_notes_images_and_exports(tmp_path: Path) -> None:
    """验证真实六页 PPT 的合并表格、备注、图片及 sidecar 完整闭包。"""

    middle_json, model_json = _analyze_sample("ppt")

    assert len(model_json.pages) == len(middle_json.pages) == 6
    assert model_json.file_suffix == middle_json.file_suffix == "ppt"
    table = next(block for block in middle_json.pages[0].blocks if isinstance(block, TableBlock))
    table_html = table.content[0].content
    soup = BeautifulSoup(table_html, "html.parser")
    rows = soup.find_all("tr")
    assert len(rows) == 9
    assert max(sum(int(cell.get("colspan", 1)) for cell in row.find_all("td")) for row in rows) == 7
    merged = sorted(
        (int(cell.get("rowspan", 1)), int(cell.get("colspan", 1)), cell.get_text(strip=True))
        for cell in soup.find_all("td")
        if cell.has_attr("rowspan") or cell.has_attr("colspan")
    )
    assert merged == sorted(
        [
            (1, 3, "Class1"),
            (1, 3, "Class2"),
            (1, 2, "A merged with B"),
            (2, 1, "R3"),
            (3, 1, "R4"),
        ]
    )
    assert [inline_text(block.content) for block in middle_json.pages[1].blocks if block.type == BlockType.PAGE_FOOTNOTE] == [
        "Some notes on the second slide."
    ]
    assert [inline_text(block.content) for block in middle_json.pages[2].blocks if block.type == BlockType.PAGE_FOOTNOTE] == [
        "Final notes on the third slide.",
        "Second line of notes.",
    ]
    assert [sum(isinstance(block, ImageBlock) for block in page.blocks) for page in middle_json.pages] == [
        0,
        0,
        0,
        2,
        0,
        0,
    ]
    chart = next(block for block in middle_json.pages[4].blocks if isinstance(block, ChartBlock))
    chart_soup = BeautifulSoup(chart.content[0].content, "html.parser")
    assert [
        [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"], recursive=False)]
        for row in chart_soup.find_all("tr")
    ] == [
        ["", "系列 1", "系列 2", "系列 3"],
        ["类别 1", "4.3", "2.4", "2"],
        ["类别 2", "2.5", "4.4", "2"],
        ["类别 3", "3.5", "1.8", "3"],
        ["类别 4", "4.5", "2.8", "5"],
    ]
    assert chart.content[0].image_base64 is not None

    page_six_lists = [block for block in model_json.pages[5] if block.get("type") == BlockType.LIST]
    assert page_six_lists[0]["attribute"] == "ordered"
    assert page_six_lists[-1]["attribute"] == "ordered"
    assert page_six_lists[-1]["start"] == 3

    export_result = export_middle_json(middle_json, tmp_path / "export")
    exported_payload = export_result.middle_json.model_dump(mode="json", exclude_none=True)
    assert export_result.json_path.exists()
    assert export_result.image_paths
    assert all(path.exists() and path.stat().st_size > 0 for path in export_result.image_paths)
    assert "image_base64" not in str(exported_payload)


def test_real_xls_recovers_tables_charts_image_link_and_exports(tmp_path: Path) -> None:
    """验证真实 XLS 的三页结构、图表、图片、链接与 sidecar 闭包。"""

    middle_json, model_json = _analyze_sample("xls")

    assert len(model_json.pages) == len(middle_json.pages) == 3
    assert model_json.file_suffix == middle_json.file_suffix == "xls"
    assert [[block.get("type") for block in page] for page in model_json.pages] == [
        [BlockType.PARAGRAPH_TITLE, BlockType.TABLE],
        [
            BlockType.PARAGRAPH_TITLE,
            BlockType.TABLE,
            BlockType.TABLE,
            BlockType.CHART,
            BlockType.TABLE,
            BlockType.CHART,
        ],
        [BlockType.PARAGRAPH_TITLE, BlockType.TABLE, BlockType.TABLE, BlockType.IMAGE],
    ]

    page_one_table = next(block for block in middle_json.pages[0].blocks if isinstance(block, TableBlock))
    page_one_soup = BeautifulSoup(page_one_table.content[0].content, "html.parser")
    page_one_rows = page_one_soup.find_all("tr")
    assert len(page_one_rows) == 9
    assert all(len(row.find_all(["th", "td"], recursive=False)) == 3 for row in page_one_rows)
    assert page_one_soup.find("a")["href"] == "http://www.baidu.com/"
    assert "21" in page_one_soup.get_text(" ", strip=True)
    assert "#NAME?" in page_one_soup.get_text(" ", strip=True)
    assert "(x+a)^n" in page_one_soup.get_text(" ", strip=True)

    chart_tables = [block.content[0].content for block in middle_json.pages[1].blocks if block.type == BlockType.CHART]
    assert [
        (
            len(BeautifulSoup(content, "html.parser").find_all("tr")),
            max(
                len(row.find_all(["th", "td"], recursive=False)) for row in BeautifulSoup(content, "html.parser").find_all("tr")
            ),
        )
        for content in chart_tables
    ] == [(5, 2), (9, 4)]

    page_three_tables = [block for block in middle_json.pages[2].blocks if isinstance(block, TableBlock)]
    assert len(page_three_tables) == 2
    assert all(
        len(
            [
                cell
                for cell in BeautifulSoup(table.content[0].content, "html.parser").find_all(["th", "td"])
                if cell.has_attr("rowspan") or cell.has_attr("colspan")
            ]
        )
        == 4
        for table in page_three_tables
    )
    assert sum(isinstance(block, ImageBlock) for block in middle_json.pages[2].blocks) == 1

    export_result = export_middle_json(middle_json, tmp_path / "export")
    exported = export_result.middle_json.model_dump(mode="json", exclude_none=True)
    assert export_result.json_path.exists()
    assert len(export_result.image_paths) == 1
    assert export_result.image_paths[0].exists()
    assert "image_base64" not in str(exported)


def test_rtf_model_parses_real_libreoffice_fixture() -> None:
    """验证真实 LibreOffice RTF 在纯 Python 路径中保留全部可见段落。"""
    with (_OFFICE_SAMPLE_DIR / "rtf_01.rtf").open("rb") as stream:
        pages = RtfModel().predict(stream)

    assert len(pages) == 1
    assert len(pages[0]) == 9
    content = "\n".join(visible_content(block.get("content")) for block in pages[0])
    assert "KVCache-centric Scheduling Algorithm" in content
    assert "Prefill Global Scheduling" in content
    assert "Conductor estimates" in content
