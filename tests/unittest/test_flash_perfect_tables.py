"""覆盖开边密排列网格的字形证据、相邻正文保护和真实公开导出。"""

from hashlib import file_digest
from os import environ
from pathlib import Path

from bs4 import BeautifulSoup
import pytest

from docvortex import parse
from docvortex.analyzers.native.pdf.models import _AxisLine, _LineItem, _PageSource
from docvortex.analyzers.native.pdf.table_detection import _detect_open_vertical_tables
from docvortex.analyzers.native.pdf.table_materialization import _materialize_table_blocks
from docvortex.render.contracts import HtmlRenderOptions, RenderMode
from test_native_pdf_table import _char_items


def _open_table_source(grouped, dx, scale, *, values=True, crossed=False, caption=True):
    """构造开边网格，组行横跨实体轨但原始表格字符各自留在格内。"""
    chars = []
    lines = []

    def add_group(entries, y):
        """把几何分离的格内容组成粗行框，模拟紧排源行合并。"""
        start = len(chars)
        content = []
        for text, left, right in entries:
            offset = len(chars)
            chars.extend(
                dict(c, char_idx=offset + i)
                for i, c in enumerate(
                    _char_items([(text, (dx + left * scale, y * scale, dx + right * scale, (y + 7) * scale))])
                )
            )
            content.append(text)
        glyphs = chars[start:]
        bbox = (min(c["bbox"][0] for c in glyphs), y * scale, max(c["bbox"][2] for c in glyphs), (y + 7) * scale)
        lines.append(_LineItem(" ".join(content), bbox, 0, len(lines), effective_height=7 * scale, chars=glyphs))

    def axis(left, top, right, bottom, orientation):
        """按相同比例移动实际纵轨和表头横线。"""
        return _AxisLine((dx + left * scale, top * scale, dx + right * scale, bottom * scale), 0.2 * scale, orientation)

    labels = (
        ("Branch", 7, 33),
        ("System", 52, 80),
        ("Decoder", 105, 137),
        ("Yield", 164, 189),
        ("Quality", 210, 240),
        ("Count", 254, 278),
    )
    if grouped:
        add_group([("Experiment metrics", 180, 260)], 41)
    add_group(labels[:2], 51 if grouped else 42)
    for label in labels[2:]:
        add_group([label], 51 if grouped else 42)
    divider = 60 if grouped else 51
    data_start = divider + 3
    height = data_start + 6 * 10
    rules = [
        axis(45.9, 39, 46.1, height, "vertical"),
        axis(151.9, 39, 152.1, height, "vertical"),
        axis(0, divider - 0.1, 287, divider + 0.1, "horizontal"),
    ]
    for index in range(6):
        y = data_start + index * 10
        title_end = 49 if crossed and index == 2 else 40
        add_group([(f"Branch {index}", 6, title_end), ("Kernel", 52, 84)], y)
        add_group([("DecoderX", 105, 141)], y)
        if values:
            for text, left, right in (("3.20", 170, 186), ("-1.07", 216, 236), ("18", 262, 272)):
                add_group([(text, left, right)], y)
        else:
            add_group([("A paragraph for ordinary readers with words continuing across every column.", 159, 278)], y)
    if caption:
        add_group([("Table 4. Station settings.", 1, 160)], height + 6)
    add_group([("The right column contains its own paragraph.", 310, 496)], data_start + 6)
    return _PageSource((660, 620), lines, chars, rules)


def _table_rows(html):
    """按实际 HTML 单元格提取文字和跨度。"""
    return [
        [(cell.get_text(strip=True), int(cell.get("rowspan", 1)), int(cell.get("colspan", 1))) for cell in row.select("td")]
        for row in BeautifulSoup(html, "html.parser").select("tr")
    ]


@pytest.mark.parametrize("grouped,dx,scale", [(False, 38, 1), (True, 66, 0.92), (True, 99, 1.13)])
def test_glyph_gutters_refine_open_rails_and_preserve_last_row(grouped, dx, scale):
    """换表头层数、坐标和字号时仍落进完整表，独立右栏不被认领。"""
    source = _open_table_source(grouped, dx, scale)
    [candidate] = _detect_open_vertical_tables(source, [])
    assert candidate.inferred_grid_authoritative
    tables, annotations, claimed = _materialize_table_blocks(source, [candidate])
    [table] = tables
    rows = _table_rows(table["content"])
    assert len(rows) == (8 if grouped else 7)
    assert [value for value, _, _ in rows[-1]] == ["Branch 5", "Kernel", "DecoderX", "3.20", "-1.07", "18"]
    assert rows[0][-1] == (("Experiment metrics", 1, 3) if grouped else ("Count", 1, 1))
    assert annotations[0]["type"] == "caption"
    assert source.lines[-1].source_index not in claimed
    assert "ordinary readers" not in table["content"]


@pytest.mark.parametrize("kwargs", [{"values": False}, {"crossed": True}, {"caption": False}])
def test_open_rails_do_not_promote_prose_or_real_crossings(kwargs):
    """只有列线但缺数值行、字形真穿线或无独立编号题注时撤销新提名。"""
    assert _detect_open_vertical_tables(_open_table_source(False, 38, 1, **kwargs), []) == []


def test_internvl_original_exports_complete_benchmarks_and_owns_captions(tmp_path):
    """整件验签后检查两个表的公开行列与列组、末行锚点、各自题注和右侧正文。"""
    path = environ.get("DOCVORTEX_TEST_INTERNVL_PDF")
    if not path:
        pytest.skip("Set DOCVORTEX_TEST_INTERNVL_PDF to the original PDF for the P045 document")
    source = Path(path)
    with source.open("rb") as stream:
        assert file_digest(stream, "sha256").hexdigest() == "a948f7499c06b8a6cb0073149f1b8f41d3acd10c4bb29fe79f250806f32e5fab"
    result = parse(source, parse_mode="txt")
    assert len(result.middle_json.pages) == 25
    dest = tmp_path / "document.html"
    result.export(dest, output_format="html", options=HtmlRenderOptions(mode=RenderMode.FULL))
    page = BeautifulSoup(dest.read_bytes(), "html.parser").select_one('section[data-page-idx="7"]')
    first, second = page.select("figure:has(table)")
    top = _table_rows(str(first.table))
    assert len(top) == 17
    assert top[0][-3:] == [("image captioning", 1, 3), ("visual question answering", 1, 4), ("dialogue", 1, 2)]
    assert top[0][1] == ("visual encoder", 2, 1)
    assert [cell for cell, _, _ in top[1]] == ["COCO", "Flickr", "NoCaps", "VQAv2", "GQA", "VizWiz", "VQAT", "MME", "POPE"]
    assert [cell for cell, _, _ in top[-1]] == [
        "InternVL-Chat (ours)",
        "IViT-6B",
        "QLLaMA",
        "Vicuna-13B",
        "336",
        "1.0B",
        "4.0M",
        "13B",
        "146.2∗",
        "92.2",
        "126.2",
        "81.2∗",
        "66.6∗",
        "58.5",
        "61.5",
        "1586.4",
        "87.6",
    ]
    lower = _table_rows(str(second.table))
    assert len(lower) == 15 and all(len(row) == 6 for row in lower)
    assert [cell for cell, _, _ in lower[-1]] == ["InternVL-G (ours)", "Cross-Attn", "QLLaMA", "128.2", "79.2", "113.7"]
    assert first.select_one(".docvortex-caption").get_text().startswith("Table 9.")
    assert second.select_one(".docvortex-caption").get_text().startswith("Table 10.")
    assert "its scaled-up parameters and datasets." in second.select_one(".docvortex-caption").get_text()
    assert any("Additionally, we leverage the XTD dataset" in node.get_text() for node in page.select("p.docvortex-text"))
    assert all("Additionally, we leverage" not in table.get_text() for table in (first, second))
