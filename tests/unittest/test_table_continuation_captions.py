"""验证续表前缀仅扩展已归组 caption，不扩大正文提升范围。"""

from typing import Any

import pytest
from _span_test_utils import inline_text
from bs4 import BeautifulSoup

from docvortex.content.table.blocks import _is_continuation_caption
from docvortex.content.table.rules import is_table_continuation_text
from docvortex.postprocess.document import model_json_to_middle_json
from docvortex.render import RenderMode, render_html, render_markdown, render_structured_content
from docvortex.schema import MiddleJson, ModelJson, TableBlock


def _table(label: str) -> dict[str, Any]:
    """构造具有相同表头、不同数据行的原始表格，避免重复数据影响断言。"""
    return {
        "type": "table",
        "bbox": [0.1, 0.15, 0.9, 0.7],
        "content": f"<table><tr><th>H</th></tr><tr><td>{label}</td></tr></table>",
    }


def _annotation(text: str, *, kind: str = "table_caption", below: bool = False) -> dict[str, Any]:
    """构造紧邻表格的文字块，用相同几何对比 caption 与正文的不同职责。"""
    return {
        "type": kind,
        "bbox": [0.1, 0.71, 0.9, 0.74] if below else [0.1, 0.11, 0.9, 0.14],
        "content": [{"type": "text", "content": text}],
    }


def _document(pages: list[list[dict[str, Any]]]) -> MiddleJson:
    """运行真实 ModelJson 后处理，并确认原始模型数据没有被修改。"""
    model = ModelJson(
        pages=pages,
        page_index_map=[],
        metadata={"file_suffix": "pdf", "producer": {"name": "test", "version": "1"}},
    )
    original = model.model_dump()
    document = model_json_to_middle_json(model)
    assert model.model_dump() == original
    return document


@pytest.mark.parametrize(
    "text",
    [
        "续表临床信息",
        "续上表临床信息",
        "续上表",
        "续表2-1 临床信息",
        "Continued Clinical information",
        "Continued growth in revenue",
        "continued7 Clinical information",
        "continued: Clinical information",
        "cont. Clinical information",
        "cont.",
        "cont’d Clinical information",
        "cont’d",
        "  ＣＯＮＴＩＮＵＥＤ：Clinical information  ",
        "\tＣＯＮＴ． Clinical information\n",
    ],
)
def test_caption_prefix_does_not_expand_raw_text_matching(text: str) -> None:
    """新增前缀只对 caption 生效，表号格式与英文 table 一词均不是前提。"""
    assert _is_continuation_caption({"content": text}) is True
    assert is_table_continuation_text(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "",
        "续航能力测试",
        "续 临床信息",
        "续",
        "Continuedgrowth",
        "cont.example",
        "cont’data",
        "…continued Clinical information",
        "Results continued below",
        "正文提到了续表，但并不是标题。",
        "（续表）临床信息",
        "(cont.) Clinical information",
    ],
)
def test_caption_prefix_keeps_non_markers_and_unexpanded_wrappers(text: str) -> None:
    """单字续、词内命中、中间位置和未约定的括号前缀不扩大识别。"""
    assert _is_continuation_caption({"content": text}) is False
    assert is_table_continuation_text(text) is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("表1（续）", True),
        ("表1（续表）", True),
        ("表1（续上表）", True),
        ("Table 1 (continued)", True),
        ("Table 1 (cont.)", True),
        ("Table 1 (cont’d)", True),
        ("Table 1 (…continued)", True),
        ("Table 1 continued", True),
        ("表1 续表", True),
        ("续表", True),
        ("continued", True),
        ("Table 1 (continued) Clinical information", True),
        ("The (continued) results need further analysis.", True),
        ("Table 1 discontinued", False),
    ],
)
def test_existing_continuation_text_rules_remain_unchanged(text: str, expected: bool) -> None:
    """固定原有后缀、括号全文匹配及 discontinued 防误判行为。"""
    assert is_table_continuation_text(text) is expected
    assert _is_continuation_caption({"content": text}) is expected


def test_caption_prefix_uses_existing_inline_text_extraction() -> None:
    """前缀跨多个样式 span 时仍按现有文本提取结果识别。"""
    caption = {
        "content": [
            {"type": "text", "content": "Con", "styles": ["bold"]},
            {"type": "text", "content": "tinued Clinical information"},
        ]
    }
    assert _is_continuation_caption(caption) is True


@pytest.mark.parametrize("prefix", ["续表", "续上表", "continued", "cont.", "cont’d"])
@pytest.mark.parametrize("below", [False, True])
def test_prefix_caption_merges_only_reading_output(prefix: str, below: bool) -> None:
    """表上和表下新前缀沿真实链路合并，逐页模式与输入仍保留物理片段。"""
    caption = f"{prefix} Clinical information"
    annotation = _annotation(caption, below=below)
    current = [_table("B"), annotation] if below else [annotation, _table("B")]
    document = _document([[_table("A"), _annotation("Previous note", kind="table_footnote", below=True)], current])
    previous_table, current_table = (page.blocks[0] for page in document.pages)
    assert isinstance(previous_table, TableBlock)
    assert isinstance(current_table, TableBlock)
    assert len(document.pages[1].blocks) == 1
    assert any(child.type == "table_footnote" for child in previous_table.content)
    assert [inline_text(child.content) for child in current_table.content if child.type == "table_caption"] == [caption]
    assert current_table.continues_prev is True

    original = document.model_dump()
    html = BeautifulSoup(render_html(document, standalone=False), "html.parser")
    assert len(html.find_all("table")) == 1
    assert [cell.get_text() for cell in html.find_all(["th", "td"])] == ["H", "A", "B"]
    markdown = render_markdown(document)
    assert "| A |" in markdown and "| B |" in markdown
    assert caption not in markdown
    assert len(BeautifulSoup(render_html(document, mode=RenderMode.FULL), "html.parser").find_all("table")) == 2
    assert caption in render_markdown(document, mode=RenderMode.FULL)
    structured = render_structured_content(document)
    assert [len(page["blocks"]) for page in structured["pages"]] == [1, 1]
    assert "| A |" in structured["pages"][0]["blocks"][0]["content"]
    assert "| B |" in structured["pages"][1]["blocks"][0]["content"]
    assert structured["pages"][1]["blocks"][0]["captions"][0]["content"] == caption
    assert document.model_dump() == original


@pytest.mark.parametrize("reason", ["width", "structure", "footnotes"])
@pytest.mark.parametrize("below", [False, True])
def test_prefix_caption_does_not_bypass_existing_table_checks(reason: str, below: bool) -> None:
    """前缀命中后仍执行既有宽度、结构和前表脚注数量检查。"""
    previous = [_table("A")]
    table = _table("B")
    caption = _annotation("续表临床信息", below=below)
    if reason == "width":
        table["bbox"][2] = caption["bbox"][2] = 0.6
    elif reason == "structure":
        table["content"] = "<table><tr><td>B1</td><td>B2</td></tr></table>"
    else:
        previous.extend(_annotation(f"Note {index}", kind="table_footnote", below=True) for index in range(2))
    current = [table, caption] if below else [caption, table]
    document = _document([previous, current])
    current_table = document.pages[1].blocks[0]
    assert isinstance(current_table, TableBlock)
    assert current_table.continues_prev is None
    assert len(BeautifulSoup(render_html(document), "html.parser").find_all("table")) == 2


@pytest.mark.parametrize(
    "text",
    [
        "续表2-1中的结果不包括对照组。",
        "续上表中的说明不适用于本组。",
        "Continued growth in revenue",
        "cont. Clinical information",
        "cont’d Clinical information",
    ],
)
@pytest.mark.parametrize("with_lines", [False, True])
def test_new_prefix_does_not_promote_leading_body_text(text: str, with_lines: bool) -> None:
    """即使正文处于页首并紧邻表格，新前缀也不能使它被提升或在渲染中丢失。"""
    body = _annotation(text, kind="text")
    if with_lines:
        body["lines"] = [{"bbox": body["bbox"][:]}]
    document = _document([[_table("A")], [body, _table("B")]])
    assert [block.type for block in document.pages[1].blocks] == ["text", "table"]
    assert inline_text(document.pages[1].blocks[0].content) == text
    assert document.pages[1].blocks[1].continues_prev is None
    original = document.model_dump()
    html = BeautifulSoup(render_html(document), "html.parser")
    assert len(html.find_all("table")) == 2
    assert text in html.get_text()
    assert text in render_markdown(document)
    assert document.model_dump() == original


def test_existing_stacked_promotion_produces_a_normal_caption() -> None:
    """沿原有标题簇规则提升的正文归组后，与直接识别的 caption 使用同一前缀判断。"""
    caption = _annotation("Clinical information", kind="caption")
    caption["bbox"] = [0.1, 0.07, 0.9, 0.10]
    fragment = _annotation("Continued Clinical information", kind="text")
    fragment["lines"] = [{"bbox": fragment["bbox"][:]}]
    document = _document([[_table("A")], [caption, fragment, _table("B")]])
    assert len(document.pages[1].blocks) == 1
    table = document.pages[1].blocks[0]
    assert isinstance(table, TableBlock)
    assert [inline_text(child.content) for child in table.content if child.type == "table_caption"] == [
        "Clinical information",
        "Continued Clinical information",
    ]
    assert table.continues_prev is True
    html = BeautifulSoup(render_html(document), "html.parser")
    assert [cell.get_text() for cell in html.find_all(["th", "td"])] == ["H", "A", "B"]
