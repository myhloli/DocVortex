"""已解析 ModelJson 的段落续接回归；仅运行 CPU 后处理和 Markdown/HTML 渲染。"""

import json
from pathlib import Path

import pytest
from _span_test_utils import inline_text
from bs4 import BeautifulSoup

from docvortex.postprocess.document import model_json_to_middle_json
from docvortex.postprocess.paragraphs import merge_para_text_blocks
from docvortex.render import render_html, render_markdown
from docvortex.schema import MiddleJson, ModelJson, TextBlock

FIXTURES = Path(__file__).parents[1] / "fixtures"


def _sample_model(name: str) -> ModelJson:
    """读取保留原始行框的真实模型夹具，不重新解析 PDF 或初始化推理模型。"""
    fixture = json.loads((FIXTURES / "paragraph_uppercase_continuation_samples.json").read_text(encoding="utf-8"))
    return ModelJson.model_validate(fixture["samples"][name]["model"])


def _starting_text(middle: MiddleJson, page: int, prefix: str) -> TextBlock:
    """按物理页和可见文字定位唯一正文，避免绑定不稳定块号。"""
    matches = [
        block
        for info in middle.pages
        if info.page_idx == page - 1
        for block in info.blocks
        if isinstance(block, TextBlock) and inline_text(block.content).startswith(prefix)
    ]
    assert len(matches) == 1, (page, prefix)
    return matches[0]


def test_saved_emnlp_cross_page_continuation_keeps_paragraphs_and_footnotes() -> None:
    """第七至八页的真实 JSON 连接句中专名，缩进新段、标题后首段与脚注仍独立。"""
    model = _sample_model("emnlp")
    original = model.model_copy(deep=True)
    middle = model_json_to_middle_json(model)
    assert model == original
    assert _starting_text(middle, 8, "TREC-Covid)").continues_prev is True
    assert not _starting_text(middle, 8, "First, we ablate").continues_prev
    assert not _starting_text(middle, 8, "To gain further insights").continues_prev
    assert not _starting_text(middle, 7, "Finally, combining").continues_prev
    markdown = render_markdown(middle)
    html = BeautifulSoup(render_html(middle), "html.parser")
    paragraphs = [" ".join(node.get_text(" ", strip=True).split()) for node in html.find_all("p")]
    joined = "neural models on TREC-Covid)"
    assert markdown.count(joined) == 1
    assert sum(joined in text for text in paragraphs) == 1
    assert markdown.count("For example, if") == 1
    assert html.get_text(" ").count("For example, if") == 1
    # 脚注开头可能含下标样式，按可见文字核对，不能把标记插入误判为正文丢失。
    note = "We have also experimented with combining BM25-QE"
    assert BeautifulSoup(markdown, "html.parser").get_text().count(note) == 1
    assert html.get_text().count(note) == 1
    assert sum(text.startswith("First, we ablate") for text in paragraphs) == 1


def test_saved_standard_demo1_single_page_keeps_order_and_joins_continuation() -> None:
    """已保存的 Standard 第十页 JSON 保持 6→7→8 顺序，仅为第七块增加续段标记。"""
    model = _sample_model("demo1")
    original = model.model_copy(deep=True)
    middle = model_json_to_middle_json(model)
    assert model == original
    text_blocks = [block for block in middle.pages[0].blocks if isinstance(block, TextBlock)]
    assert [block.index for block in text_blocks] == [6, 7, 8]
    assert _starting_text(middle, 10, "Lambrechtsbos B appear").continues_prev is True
    assert not _starting_text(middle, 10, "The model fits show").continues_prev
    markdown = render_markdown(middle)
    html = BeautifulSoup(render_html(middle), "html.parser")
    paragraphs = [" ".join(node.get_text(" ", strip=True).split()) for node in html.find_all("p")]
    joined = "Low flows at Lambrechtsbos B"
    assert markdown.count(joined) == 1
    assert sum(joined in text for text in paragraphs) == 1
    assert sum(text.startswith("The model fits show") for text in paragraphs) == 1


def test_saved_basic_demo1_sentence_end_joins_across_chart() -> None:
    """Basic 第七页凭缩进与跨栏几何接续公式句号后的正文，并核对图表和导出段落。"""
    model = _sample_model("demo1_basic_page7")
    original = model.model_copy(deep=True)
    middle = model_json_to_middle_json(model)
    assert model == original
    page = middle.pages[0]
    assert page.page_idx == 6
    assert [block.index for block in page.blocks] == [0, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11]
    assert [block.index for block in page.blocks if isinstance(block, TextBlock) and block.continues_prev] == [8]
    previous = next(block for block in page.blocks if block.index == 5)
    assert previous.content[-1].type == "equation_inline"
    assert previous.content[-1].content.endswith(".")

    markdown = render_markdown(middle)
    html = BeautifulSoup(render_html(middle), "html.parser")
    paragraphs = [" ".join(node.get_text(" ", strip=True).split()) for node in html.find_all("p")]
    for rendered_paragraphs in (markdown.split("\n\n"), paragraphs):
        joined = [text for text in rendered_paragraphs if text.startswith("Following the successful fitting")]
        assert len(joined) == 1
        assert "values are given in Table 4." in joined[0]
        assert "particularly for the 20 years FDC." in joined[0]
        assert sum(text.startswith("The relative net flow change") for text in rendered_paragraphs) == 1
    caption = "Fig. 3. Examples of observed and flow duration curves"
    assert markdown.count(caption) == 1
    assert html.get_text().count(caption) == 1


def test_saved_single_emnlp_page_never_invents_missing_previous_page() -> None:
    """只提供第八页 JSON 时保留独立开头，不能因有缩进证据而虚构前页正文。"""
    model = _sample_model("emnlp")
    model = model.model_copy(update={"pages": model.pages[1:], "page_index_map": [7]}, deep=True)
    middle = model_json_to_middle_json(model)
    assert not _starting_text(middle, 8, "TREC-Covid)").continues_prev
    markdown = render_markdown(middle)
    assert "neural models on TREC-Covid)" not in markdown
    assert markdown.count("TREC-Covid), the rank fusion") == 1


def test_saved_demo1_continuation_is_independent_of_other_page_availability() -> None:
    """给同一单页 JSON 增加其他页上下文不会改变本页的续段和语义内容。"""
    model = _sample_model("demo1")
    batched = model.model_copy(update={"pages": [[], *model.pages, []], "page_index_map": [8, 9, 10]}, deep=True)
    standalone = model_json_to_middle_json(model)
    together = model_json_to_middle_json(batched)
    assert standalone.pages[0] == together.pages[1]
    assert "Low flows at Lambrechtsbos B" in render_markdown(standalone)


@pytest.mark.parametrize("case_index", range(5))
def test_real_mixed_page_regions_do_not_gain_false_continuations(case_index: int) -> None:
    """人工原页确认的作者、摘要、参考条目、表格标签和公式说明继续保持独立。"""
    fixture = json.loads((FIXTURES / "paragraph_uppercase_layout_regressions.json").read_text(encoding="utf-8"))
    case = fixture["cases"][case_index]
    page = case["page"]
    merge_para_text_blocks([page])
    for index in case["expected_independent_indices"]:
        block = next(block for block in page["blocks"] if block["index"] == index)
        assert not block.get("continues_prev", False), (case["name"], index)
