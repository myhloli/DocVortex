"""按人工裁决与原页事实验证本轮原生 Flash 修复，不以候选结果生成期望。"""

from functools import lru_cache
from io import BytesIO
from pathlib import Path
import re

import pytest
from bs4 import BeautifulSoup
from reportlab.pdfgen.canvas import Canvas

from _flash_pdf_test_utils import _visible_text
from docvortex import parse
from docvortex.document.pdf import PDFDocument

# 根据原页逐项抄录，固定续行归属与全文，不能从候选列表生成期望。
LETTERED_ITEMS = [
    "a. The size of the field of view decreases",
    "b. The field of view becomes darker",
    "c. The size of the image increases",
    "d. The resolution (ability to see detail) increases",
    "e. The working distance between the slide and the objective lens decreases",
    "f. The depth of focus (thickness of the specimen that is visible) is reduced",
]
BULLETED_ITEMS = [
    [
        "Check tidal conditions beforehand",
        "Stay within marked channels",
        "Pay attention to buoys and markers",
        "Do not run aground",
        "If you run aground, call for help",
        "Wear polarized sunglasses",
        "Take a safe boating course",
    ],
    [
        "Do careful mapping of seagrass in potential areas for development",
        "Avoid dredging and filling",
        "Learn about existing regulations",
    ],
    [
        "Diminish fertilizer use (use soaking, rain gardens, and native plants instead)",
        "Dispose of pet waste properly",
        "Keep seagrass in mind during construction (for example, build high docks with grating instead of planks)",
    ],
    [
        "Urge politicians to establish stricter water quality regulations",
        "Mobilize to give seagrass an 'endangered' status",
        "Follow established laws for seagrass protection",
        "Reach out to environmental organizations and volunteer in restoration projects",
        "Challenge the misconception that seagrass is 'ugly' and 'useless'",
        "Tell your friends and family about the importance of this ecosystem",
    ],
]


def _normalized_item_text(text):
    """只统一空白与编号后的间隔，保留每个条目的完整文字和标点。"""
    return re.sub(r"^([a-f])\.\s*", r"\1. ", " ".join(text.split()))


@lru_cache(maxsize=None)
def _blocks(sample):
    """解析完整原件并返回公开首页面，复用上一轮已有原件而不重复复制。"""
    root = Path(__file__).parent / "pdfs"
    path = root / "flash_review_20261004" / f"review_{sample}.pdf"
    if not path.exists():
        path = root / "flash_review_20261003" / f"review_{sample}.pdf"
    return parse(path, keep_model_json=True).to_dict()["pages"][0]["blocks"]


@pytest.mark.parametrize("sample", [79, 80])
def test_later_full_page_fill_removes_covered_header_and_number(sample):
    """原页后绘制的整页填充完全盖住旧页眉和页码，它们不能进入可见正文。"""
    blocks = _blocks(sample)
    assert not any("Jailed for Doing Business" in _visible_text(b["content"]) for b in blocks)
    assert not any(b["type"] == "text" and b["bbox"][1] > 0.93 for b in blocks)
    assert any("regulatory" in _visible_text(b["content"]) for b in blocks)


def test_drop_cap_retains_complete_first_word_and_paragraph():
    """下沉I属于首词India，不能丢字、插空格或独立成段。"""
    matches = [b for b in _blocks(79) if "suffers from" in _visible_text(b["content"])]
    assert len(matches) == 1
    text = _visible_text(matches[0]["content"])
    assert text.startswith("India suffers") and text.endswith("wealth and GDP.")
    assert matches[0]["type"] == "text"


def test_hanging_letter_item_keeps_five_lines_and_inline_bold():
    """d项的五行是一个连续段，粗体短语仍是行内样式而非独立标题。"""
    matches = [b for b in _blocks(67) if "Awareness of Plastics Ordinance" in _visible_text(b["content"])]
    assert len(matches) == 1
    block = matches[0]
    assert block["type"] == "text"
    assert _visible_text(block["content"]).endswith("not aware of the ordinance.")
    assert "Waste Management" not in _visible_text(block["content"])
    assert any(
        "Awareness of Plastics Ordinance" in s.get("content", "") and "bold" in s.get("styles", []) for s in block["content"]
    )
    # 相邻b项原本连续，不能因末行斜体字体不同而把段尾切走。
    budget = next(b for b in _blocks(67) if "Waste Management Budget" in _visible_text(b["content"]))
    assert _visible_text(budget["content"]).endswith("budget. See Figure 20.")


def test_single_lettered_run_becomes_six_local_list_items():
    """连续a-f区域恢复六项，外侧操作及跨照片的分散编号仍保留原有正文结构。"""
    lists = [b for b in _blocks(115) if b["type"] == "list"]
    assert len(lists) == 1
    items = lists[0]["content"]
    assert len(items) == 6
    assert all(item["type"] == "text" for item in items)
    assert [_normalized_item_text(_visible_text(item["content"])) for item in items] == LETTERED_ITEMS


def test_vector_bullets_keep_four_lists_and_complete_continuations():
    """四组原生圆点路径证明条目边界，折行不能被误当作新的条目。"""
    lists = [b for b in _blocks(163) if b["type"] == "list"]
    assert [len(b["content"]) for b in lists] == [7, 3, 3, 6]
    assert [
        [_normalized_item_text(_visible_text(item["content"])) for item in block["content"]] for block in lists
    ] == BULLETED_ITEMS
    assert all(item["type"] == "text" for b in lists for item in b["content"])


def test_six_isolated_icons_and_koala_have_unique_images():
    """六个左侧图标及右上考拉完整保留，不能因为没有两条文本标签而丢弃。"""
    images = [b for b in _blocks(103) if b["type"] == "image"]
    left = [b for b in images if b["bbox"][2] < 0.30]
    assert len(left) == 6
    assert len([b for b in images if b["bbox"][0] > 0.80 and b["bbox"][1] < 0.1]) == 1
    assert len(images) == 7
    koala = next(b for b in images if b["bbox"][0] > 0.80 and b["bbox"][1] < 0.1)
    assert koala["bbox"][2] >= 0.999 and koala["bbox"][1] <= 0.001
    assert all(b["bbox"][2] - b["bbox"][0] < 0.3 for b in images)
    assert any("Summarize or extract" in _visible_text(b["content"]) for b in _blocks(103) if b["type"] == "text")


def test_native_spreadsheet_grid_precedes_figure_caption_recovery():
    """原生六列十六行网格必须是表格，不能被邻近Figure图题抢先认领为图片。"""
    tables = [b for b in _blocks(128) if b["type"] == "table"]
    assert len(tables) == 1
    body = next(b for b in tables[0]["content"] if b["type"] == "table_body")
    html = _visible_text(body["content"])
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("tr")
    assert len(rows) == 16
    assert all(len(row.find_all(["td", "th"])) == 6 for row in rows)
    assert [c.get_text(" ", strip=True) for c in rows[0].find_all(["td", "th"])][1:] == list("ABCDE")
    assert "Upper Confidence" in soup.get_text(" ") and "26.75" in soup.get_text(" ")
    assert len([b for b in _blocks(128) if b["type"] == "image"]) == 1


def test_parallel_three_column_panels_keep_heading_with_its_body():
    """三栏几何面板按栏输出，栏目标题紧接本栏正文而不是三个标题先并排。"""
    texts = [_visible_text(b["content"]) for b in _blocks(181)]
    anchors = [
        "Our Purpose",
        "Making AI Beneficial",
        "Our Mission",
        "Easy-to-apply AI, Everywhere",
        "What We Do",
        "Providing the world’s best",
    ]
    positions = [next(i for i, text in enumerate(texts) if anchor in text) for anchor in anchors]
    assert positions == sorted(positions)


def _occlusion_pdf(kind, scale, shift):
    """构造遮挡先后、透明、局部覆盖及非矩形路径的匹配反例。"""
    stream = BytesIO()
    c = Canvas(stream, pagesize=(300 * scale, 220 * scale))
    c.translate(shift, shift)
    c.scale(scale, scale)
    if kind == "before":
        c.rect(10, 80, 180, 50, fill=1, stroke=0)
    c.setFont("Helvetica", 10)
    c.drawString(20, 100, "KEEP_OR_COVER")
    if kind != "before":
        c.setFillColorRGB(0.2, 0.2, 0.2)
        if kind == "transparent":
            c.setFillAlpha(0.3)
        if kind == "nearly_opaque":
            c.setFillAlpha(0.999)
        if kind == "blend":
            c.setBlendMode("Multiply")
        if kind == "partial":
            c.rect(20, 90, 10, 30, fill=1, stroke=0)
        elif kind == "ellipse":
            c.ellipse(10, 80, 190, 130, fill=1, stroke=0)
        else:
            c.rect(10, 80, 180, 50, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1)
    c.drawString(20, 110, "AFTER")
    c.save()
    return stream.getvalue()


@pytest.mark.parametrize("scale,shift", [(0.7, 0), (1, 23), (1.6, 47)])
@pytest.mark.parametrize("kind", ["covered", "before", "transparent", "nearly_opaque", "partial", "ellipse", "blend"])
def test_complete_opaque_rectangle_is_required_for_text_occlusion(kind, scale, shift):
    """只有后绘制的完整不透明矩形可排除旧文字，其他情况保守保留。"""
    with PDFDocument(_occlusion_pdf(kind, scale, shift)) as document:
        raw = "".join(c["char"] for c in document.get_page_chars(0))
        visible = "".join(c["char"] for c in document._extract_native_page(0).text_geometry.chars)
    assert "KEEP_OR_COVER" in raw
    assert ("KEEP_OR_COVER" in visible) == (kind != "covered")
    assert "AFTER" in visible


@pytest.mark.parametrize("scale,left,font", [(0.6, 20, "GenericA"), (1, 100, "Subset+B"), (1.8, 250, "OtherFamily")])
@pytest.mark.parametrize("kind", ["continuous", "gap", "font", "heading", "inline"])
def test_hanging_item_requires_continuous_body_lane(scale, left, font, kind):
    """变化位置字号字体后仍连接同项；真实留白、字体及标题重置拒绝连接。"""
    from docvortex.analyzers.native.pdf.local_text import group_hanging_letter_paragraphs
    from docvortex.analyzers.native.pdf.models import _LineItem

    marker = _LineItem(
        "c." if kind != "inline" else "ordinary c. prose",
        (left, 50, left + 7 * scale, 50 + 10 * scale),
        0,
        0,
        effective_height=10 * scale,
    )
    rows = [
        _LineItem(
            text,
            (left + 20 * scale, 50 + i * 12 * scale, left + 160 * scale, 50 + (10 + i * 12) * scale),
            0,
            i + 1,
            effective_height=10 * scale,
            font_signature=(font, 0),
        )
        for i, text in enumerate(
            [
                "First body words have stable geometry.",
                "A second complete sentence stays here.",
                "Third body line finishes the same item.",
            ]
        )
    ]
    if kind == "gap":
        rows[-1].bbox = (rows[-1].bbox[0], rows[-1].bbox[1] + 20 * scale, rows[-1].bbox[2], rows[-1].bbox[3] + 20 * scale)
    if kind == "font":
        rows[-1].font_signature = ("Different", 0)
    if kind == "heading":
        rows[-1].semantic_type = "paragraph_title"
    group_hanging_letter_paragraphs([marker, *rows])
    assert all(line.paragraph_group is not None for line in [marker, *rows]) == (kind == "continuous")


@pytest.mark.parametrize("scale,left,font", [(0.65, 15, "AnySerif"), (1, 90, "Subset+Plain"), (1.7, 200, "Other")])
@pytest.mark.parametrize("kind", ["letters", "missing_marker", "gap", "indent", "dispersed", "inline"])
def test_local_lists_require_one_contiguous_region(scale, left, font, kind):
    """变换字号栏宽位置与字体后局部列表仍成立；缺编号、留白、错缩进及分散块拒绝聚合。"""
    from docvortex.analyzers.native.pdf.local_lists import split_local_list_blocks
    from docvortex.analyzers.native.pdf.models import _LineItem

    rows = [
        _LineItem(
            f"{chr(97 + i)}. Body words for an item",
            (left, (50 + 12 * i) * scale, left + 180 * scale, (60 + 12 * i) * scale),
            0,
            i,
            effective_height=10 * scale,
            font_signature=(font, 0),
        )
        for i in range(3)
    ]
    if kind == "missing_marker":
        rows[1].text = "ordinary continuation"
    if kind == "gap":
        rows[2].bbox = (left, 110 * scale, left + 180 * scale, 120 * scale)
    if kind == "indent":
        rows[1].bbox = (left + 8 * scale, *rows[1].bbox[1:])
    if kind == "inline":
        rows[0].text = "Prose a. with an inline enumeration"
    blocks = [
        {"type": "text", "content": "joined", "bbox": (left, 50 * scale, left + 180 * scale, 84 * scale), "_text_lines": rows}
    ]
    if kind == "dispersed":
        blocks = [{**blocks[0], "_text_lines": [row]} for row in rows]
    result = split_local_list_blocks(blocks, [])
    assert any(b["type"] == "list" for b in result) == (kind == "letters")


@pytest.mark.parametrize("sample", [115, 163])
def test_local_list_html_preserves_separate_items_and_continuations(tmp_path, sample):
    """公开HTML逐项换行并完整保留续行，六项字母列表与四组圆点列表均不得折叠。"""
    path = Path(__file__).parent / f"pdfs/flash_review_20261004/review_{sample}.pdf"
    if not path.exists():
        path = Path(__file__).parent / f"pdfs/flash_review_20261003/review_{sample}.pdf"
    result = parse(path)
    destination = tmp_path / "document.html"
    result.export(destination, output_format="html")
    soup = BeautifulSoup(destination.read_text(), "html.parser")
    expected = LETTERED_ITEMS if sample == 115 else [text for group in BULLETED_ITEMS for text in group]
    assert [_normalized_item_text(li.get_text(" ")) for li in soup.find_all("li")] == expected


@pytest.mark.parametrize("scale,left,font", [(0.7, 18, "Plain"), (1, 70, "Subset+Other"), (1.6, 170, "Third")])
@pytest.mark.parametrize("kind", ["graphic", "prose", "rule", "corner", "raster"])
def test_independent_vector_graphic_requires_complex_isolated_component(scale, left, font, kind):
    """改变字号位置字体，独立复杂图保留；正文、细线、整页角背景及已有图片拒绝重复认领。"""
    from types import SimpleNamespace
    from docvortex.analyzers.native.pdf.models import _LineItem
    from docvortex.analyzers.native.pdf.isolated_graphics import isolated_vector_components

    box = (left, 50 * scale, left + 45 * scale, 110 * scale)
    if kind == "rule":
        box = (left, 50 * scale, left + 45 * scale, 51 * scale)
    if kind == "corner":
        box = (0, 0, 45 * scale, 60 * scale)
    path = SimpleNamespace(
        bbox=box,
        form_depth=0,
        fill_visible=True,
        stroke_visible=False,
        rectangle_bboxes=(),
        segment_count=40,
        fill_rgba=(40, 60, 80, 255),
    )
    line = _LineItem(
        "normal body words",
        (left + 100 * scale, 50 * scale, left + 220 * scale, 60 * scale),
        0,
        0,
        effective_height=10 * scale,
        font_signature=(font, 0),
    )
    if kind == "prose":
        line.bbox = (left + 5 * scale, 70 * scale, left + 40 * scale, 80 * scale)
    source = SimpleNamespace(
        page_size=(800 * scale, 700 * scale), path_infos=[path], lines=[line], image_bboxes=[box] if kind == "raster" else []
    )
    assert bool(isolated_vector_components(source)) == (kind == "graphic")


@pytest.mark.parametrize(
    "scale,left,width,font", [(0.65, 12, 120, "Plain"), (1, 70, 180, "Subset+Other"), (1.7, 130, 230, "Third")]
)
@pytest.mark.parametrize("kind", ["closed", "open", "few_rows", "figure_form"])
def test_grid_protection_requires_native_closed_rows_and_columns(scale, left, width, font, kind):
    """变换位置字号栏宽字体，闭合文字网格优先；开边、少行及组合图内示意网格不保护。"""
    from types import SimpleNamespace
    from docvortex.analyzers.native.pdf.models import _LineItem, _AxisLine, _TableCandidate
    from docvortex.analyzers.native.pdf.table_detection import has_native_closed_grid

    top, height = 50 * scale, 48 * scale
    box = (left, top, left + width * scale, top + height)
    rules = [_AxisLine((left, top + i * 12 * scale, box[2], top + i * 12 * scale), 0.5, "horizontal") for i in range(5)]
    rules += [
        _AxisLine((left + i * width * scale / 3, top, left + i * width * scale / 3, box[3]), 0.5, "vertical") for i in range(4)
    ]
    rows = [
        _LineItem(
            f"{i} {j}",
            (
                left + (j * width / 3 + 4) * scale,
                top + (i * 12 + 1) * scale,
                left + (j * width / 3 + 20) * scale,
                top + (i * 12 + 11) * scale,
            ),
            0,
            i * 3 + j,
            effective_height=10 * scale,
            font_signature=(font, 0),
        )
        for i in range(4)
        for j in range(3)
    ]
    if kind == "open":
        rules = [r for r in rules if not (r.orientation == "vertical" and r.bbox[0] == left)]
    if kind == "few_rows":
        rows = rows[:3]
    source = SimpleNamespace(
        lines=rows,
        drawing_lines=rules,
        form_bboxes=[(left - 10 * scale, top - 10 * scale, box[2] + 100 * scale, box[3] + 100 * scale)]
        if kind == "figure_form"
        else [],
    )
    candidate = _TableCandidate(box, box, 0, 100, core_bbox=box, line_indices={row.source_index for row in rows})
    assert has_native_closed_grid(source, candidate) == (kind == "closed")


@pytest.mark.parametrize("scale,left,font", [(0.7, 10, "Plain"), (1, 80, "Other"), (1.6, 180, "Subset+Third")])
@pytest.mark.parametrize("kind", ["panels", "crossing", "missing_body", "reset", "font"])
def test_parallel_panel_groups_require_headings_gutters_and_complete_bodies(scale, left, font, kind):
    """变换字号位置字体后同高三栏仍分组；跨栏块、缺正文、标题重置及不同字体拒绝。"""
    from docvortex.analyzers.native.pdf.panel_order import parallel_heading_panel_groups

    titles = [
        {
            "type": "paragraph_title",
            "bbox": (left + i * 100 * scale, 50 * scale, left + (i * 100 + 40) * scale, 60 * scale),
            "_font_signatures": {(font, 0)},
        }
        for i in range(3)
    ]
    bodies = [
        {"type": "text", "bbox": (left + i * 100 * scale, 75 * scale, left + (i * 100 + 70) * scale, (100 + i * 20) * scale)}
        for i in range(3)
    ]
    blocks = [*titles, *bodies]
    if kind == "crossing":
        blocks.append({"type": "text", "bbox": (left, 110 * scale, left + 250 * scale, 120 * scale)})
    if kind == "missing_body":
        blocks.pop()
    if kind == "reset":
        titles[1]["bbox"] = (titles[1]["bbox"][0], 70 * scale, titles[1]["bbox"][2], 80 * scale)
    if kind == "font":
        titles[1]["_font_signatures"] = {("Different", 0)}
    assert len(parallel_heading_panel_groups(blocks, set())) == (3 if kind == "panels" else 0)


@pytest.mark.parametrize("scale,left,font", [(0.7, 10, "AnySerif"), (1, 90, "Subset+Other"), (1.6, 200, "Third")])
@pytest.mark.parametrize("kind", ["drop_cap", "real_space", "small", "far_suffix", "no_return", "uppercase"])
def test_drop_cap_word_repair_requires_character_geometry_and_body_wrap(scale, left, font, kind):
    """变换位置字号字体，只删除下沉首字母旁零面积生成空白；真实空格及证据不足保留。"""
    from docvortex.analyzers.native.pdf.models import _LineItem
    from docvortex.analyzers.native.pdf.local_text import restore_drop_cap_paragraphs

    cap = (left, 0, left + 20 * scale, (20 if kind == "small" else 60) * scale)
    blank = (left, 40 * scale, left + (3 * scale if kind == "real_space" else 0), 40 * scale)
    chars = [{"char": "A", "char_idx": 0, "bbox": cap}, {"char": " ", "char_idx": 1, "bbox": blank}]
    for i, letter in enumerate("REA" if kind == "uppercase" else "rea"):
        x = left + ((70 if kind == "far_suffix" else 24) + i * 5) * scale
        chars.append({"char": letter, "char_idx": i + 2, "bbox": (x, 22 * scale, x + 5 * scale, 32 * scale)})
    chars.append({"char": " ", "char_idx": 5, "bbox": (left + 39 * scale, 22 * scale, left + 42 * scale, 32 * scale)})
    first = _LineItem(
        "A rea first body words",
        (left, 0, left + 120 * scale, 60 * scale),
        0,
        0,
        chars=chars,
        effective_height=10 * scale,
        font_signature=(font, 0),
    )
    rows = [
        _LineItem(
            "continuous body row with words",
            (
                left + (24 if i < 2 or kind == "no_return" else 1) * scale,
                (34 + i * 12) * scale,
                left + 120 * scale,
                (44 + i * 12) * scale,
            ),
            0,
            i + 1,
            effective_height=10 * scale,
            font_signature=(font, 0),
        )
        for i in range(4)
    ]
    restore_drop_cap_paragraphs([first, *rows])
    assert first.text.startswith("Area ") == (kind == "drop_cap")


def test_spreadsheet_all_cells_match_visible_source():
    """原页逐格人工可读数值作为独立期望，验证编号、表头及空单元格没有遗漏。"""
    table = next(b for b in _blocks(128) if b["type"] == "table")
    body = next(b for b in table["content"] if b["type"] == "table_body")
    soup = BeautifulSoup(_visible_text(body["content"]), "html.parser")
    actual = [[cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])] for row in soup.find_all("tr")]
    expected = [
        ["", "A", "B", "C", "D", "E"],
        ["1", "time", "observed", "Forecast(observed)", "Lower Confidence Bound(observed)", "Upper Confidence Bound(observed)"],
    ]
    expected += [
        [str(i + 2), str(i), observed, "", "", ""] for i, observed in enumerate(["13", "12", "13.5", "15", "16", "18", "17.5"])
    ]
    expected.append(["9", "7", "17.9", "17.90", "17.90", "17.90"])
    expected += [
        [str(i + 10), str(i + 8), "", *values]
        for i, values in enumerate(
            [
                ["19.73214458", "17.99", "21.47"],
                ["21.59962998", "19.81", "23.39"],
                ["21.62645857", "19.78", "23.47"],
                ["22.85993116", "20.96", "24.76"],
                ["24.72741656", "22.78", "26.68"],
                ["24.75424515", "22.75", "26.75"],
            ]
        )
    ]
    assert actual == expected
