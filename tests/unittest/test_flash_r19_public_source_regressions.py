"""用两份完整原件回归已披露的 Flash 正文续读和卡片归属错误。"""

from hashlib import file_digest
from itertools import product
import json
from os import environ
from pathlib import Path
import re
import unicodedata

from bs4 import BeautifulSoup
from PIL import Image
import pytest

from docvortex import parse

_FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/flash_r19_public_source_regressions.json").read_text())
_ROUTES = (
    pytest.param({}, id="default"),
    pytest.param(dict(parse_mode="txt", pdf_structure="auto"), id="native"),
)
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]\n]*\]\(([^\s)]+)\)")


def _normalized(text):
    """统一 Unicode 和空白，保留词序与错误拆开的词。"""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _export_source(case, env_name, route, output):
    """校验官方整本身份，走公开入口生成实际 HTML 和 Markdown。"""
    source_file = environ.get(env_name)
    if not source_file:
        pytest.skip(f"Set {env_name} to the original PDF from {case['source_url']}")
    with Path(source_file).open("rb") as source:
        assert file_digest(source, "sha256").hexdigest() == case["sha256"], case["source_url"]
    result = parse(Path(source_file), **route)
    pages = result.to_dict()["pages"]
    assert len(pages) == case["pages"]
    page = pages[case["physical_page"] - 1]
    assert page["page_idx"] == case["physical_page"] - 1
    html = output / "document.html"
    markdown = output / "document.md"
    result.export(html, output_format="html")
    result.export(markdown, output_format="markdown")
    dom = BeautifulSoup(html.read_text(), "html.parser")
    nodes = dom.select(f'.docvortex-block[data-page-idx="{page["page_idx"]}"]')
    return {str(block["index"]): block for block in page["blocks"]}, nodes, markdown.read_text()


def _inside(bbox, region):
    """按物理文本框中心限定原页正文或卡片区域。"""
    x = (bbox[0] + bbox[2]) / 2
    y = (bbox[1] + bbox[3]) / 2
    return region[0] <= x <= region[2] and region[1] <= y <= region[3]


def _assert_prose_anchors(paragraphs, cases):
    """各组跨印刷行的锚点须在同一公开段中唯一、顺序正确。"""
    combined = " ".join(paragraphs)
    ordered = []
    for group in cases:
        anchors = [_normalized(anchor) for anchor in group]
        missing_or_repeated = [anchor for anchor in anchors if combined.count(anchor) != 1]
        assert not missing_or_repeated, f"跨行锚点缺失、错拼或重复 {missing_or_repeated}"
        positions = [index for index, paragraph in enumerate(paragraphs) if all(anchor in paragraph for anchor in anchors)]
        assert len(positions) == 1, f"源段跨印刷行被拆散 {group}"
        ordered.extend(combined.index(anchor) for anchor in anchors)
    assert ordered == sorted(ordered), "跨行与跨栏正文顺序改变"


@pytest.mark.parametrize("route", _ROUTES)
def test_leica_print_lines_remain_in_article_reading_order(route, tmp_path):
    """左半裁图调整后，右三栏的实际正文继续按印刷内容阅读。"""
    case = _FIXTURE["leica"]
    blocks, nodes, markdown = _export_source(case, "DOCVORTEX_TEST_LEICA_COURRIER_109", route, tmp_path)
    prose = [
        _normalized(node.get_text(" ", strip=True))
        for node in nodes
        if (block := blocks.get(node["data-block-index"]))
        and block["type"] == "text"
        and _inside(block["bbox"], case["body_region"])
    ]
    _assert_prose_anchors(prose, case["paragraph_anchors"])

    first, last = case["article_bounds"]
    start = markdown.index(first)
    end = markdown.index(last, start)
    markdown_prose = [_normalized(paragraph) for paragraph in re.split(r"\n[ \t]*\n", markdown[start:end])]
    _assert_prose_anchors(markdown_prose, case["paragraph_anchors"])


def _overlap_ratio(first, second):
    """计算成品与原始纸面卡框的交并比，分辨单卡和跨行长图。"""
    width = max(0, min(first[2], second[2]) - max(first[0], second[0]))
    height = max(0, min(first[3], second[3]) - max(first[1], second[1]))
    both = width * height
    union = (first[2] - first[0]) * (first[3] - first[1]) + (second[2] - second[0]) * (second[3] - second[1]) - both
    return both / union


def _contains_label(text, anchor):
    """以短独立词组匹配卡片题签，避免词内命中。"""
    return re.search(rf"(?<!\w){re.escape(anchor)}(?!\w)", text) is not None


def _card_assets(blocks, nodes, case, output):
    """从纸面卡位读取独立父图、真实图片和公开文字归属。"""
    columns, rows = case["columns"], case["rows"]
    region = [columns[0][0], rows[0][0], columns[-1][1], rows[-1][1]]
    anchors = case["row_major_anchors"]
    cards = []
    for node in nodes:
        block = blocks.get(node["data-block-index"])
        if not block or block["type"] != "image" or not _inside(block["bbox"], region):
            continue
        if not (details := node.select_one("figure details")):
            continue
        text = _normalized(details.get_text(" ", strip=True))
        labels = [label for label in anchors if _contains_label(text, label)]
        if labels:
            images = node.select("figure img[src]")
            assert len(images) == 1, f"同一单卡有多个公共图像 {labels}"
            cards.append((labels, block["bbox"], images[0]["src"]))
    assert len(cards) == len(anchors), f"预期 {len(anchors)} 张有独立题签的卡片，实际 {len(cards)} 张"
    expected_boxes = ([x[0], y[0], x[1], y[1]] for y, x in product(rows, columns))
    for (labels, bbox, path), anchor, expected in zip(cards, anchors, expected_boxes, strict=True):
        assert labels == [anchor], f"卡片归属或顺序不符 {labels} != {anchor}"
        assert _overlap_ratio(bbox, expected) >= 0.7, f"卡片图跨越纸面相邻卡位 {anchor}, {bbox}"
        with Image.open(output / path) as image:
            image.load()
    paths = [card[2] for card in cards]
    assert len(set(paths)) == len(anchors), "多个卡位引用同一公共资产路径"
    return paths


@pytest.mark.parametrize("route", _ROUTES)
def test_rogers_governance_cards_keep_independent_assets_and_row_order(route, tmp_path):
    """四行三列治理卡保留逐卡题签、独立可解引用资产及逐行次序。"""
    case = _FIXTURE["rogers"]
    blocks, nodes, markdown = _export_source(case, "DOCVORTEX_TEST_ROGERS_2013_ANNUAL_REPORT", route, tmp_path)
    paths = _card_assets(blocks, nodes, case, tmp_path)

    images = list(_MARKDOWN_IMAGE.finditer(markdown))
    expected = dict(zip(paths, case["row_major_anchors"], strict=True))
    actual = []
    for index, image in enumerate(images):
        path = image.group(1)
        if path not in expected:
            continue
        actual.append(path)
        end = images[index + 1].start() if index + 1 < len(images) else len(markdown)
        details = BeautifulSoup(markdown[image.end() : end], "html.parser").find("details")
        assert details and _contains_label(_normalized(details.get_text(" ", strip=True)), expected[path])
    assert actual == paths, "Markdown 各卡图件引用的顺序或出现次数与实际纸面不符"
