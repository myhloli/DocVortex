"""验证 PDF 输出清洗的文字范围、结构保护与跨节点公式边界。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from bs4 import BeautifulSoup
import pytest

from docgale.content import normalize_pdf_model_text
from docgale.schema import BlockType, RAW_ALGORITHM, RAW_CAPTION, RAW_FOOTNOTE, RAW_PHONETIC


@pytest.mark.parametrize(
    "kind",
    [
        BlockType.TEXT,
        BlockType.DOC_TITLE,
        BlockType.PARAGRAPH_TITLE,
        BlockType.ASIDE_TEXT,
        BlockType.HEADER,
        BlockType.FOOTER,
        BlockType.PAGE_NUMBER,
        BlockType.PAGE_FOOTNOTE,
        BlockType.REF_TEXT,
        BlockType.LIST,
        BlockType.INDEX,
        RAW_CAPTION,
        RAW_FOOTNOTE,
        RAW_PHONETIC,
        BlockType.IMAGE_CAPTION,
        BlockType.TABLE_FOOTNOTE,
    ],
)
def test_natural_language_maps_only_fullwidth_alphanumeric(kind: str) -> None:
    """迁移旧清洗范围，保留标点、全角空格、圈号、兼容单位与组合字符。"""
    model = [[{"type": kind, "content": "Ａｚ０，。！？（）　①㎏Ⅲﬀ²e\u0301", "bbox": [0, 0, 1, 1], "angle": 90}]]
    expected = deepcopy(model)
    expected[0][0]["content"] = "Az0，。！？（）　①㎏Ⅲﬀ²e\u0301"
    assert normalize_pdf_model_text(model) is None
    assert model == expected
    normalize_pdf_model_text(model)
    assert model == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (r"前Ａ１ \(Ｆ２+x\) 中Ｂ３ <eq>Ｃ４+y</eq> 后Ｄ５", r"前A1 \(Ｆ２+x\) 中B3 <eq>C4+y</eq> 后D5"),
        (r"前Ａ１ \(Ｆ２ 后Ｂ３", r"前A1 \(Ｆ２ 后Ｂ３"),
        (r"前Ａ１ <eq>Ｆ２ 后Ｂ３", r"前A1 <eq>F2 后B3"),
        ("Ａ \\[Ｆ２\n＋Ｇ３\\] Ｂ", "A \\[Ｆ２\n＋Ｇ３\\] B"),
    ],
)
def test_literal_formula_delimiters_are_preserved(source: str, expected: str) -> None:
    """保留已有公式定界符及未闭合公式，不把普通字符串当 HTML 解释。"""
    model = [[{"type": "text", "content": source}]]
    normalize_pdf_model_text(model)
    assert model[0][0]["content"] == expected


def test_span_boundaries_styles_and_urls_are_preserved() -> None:
    """公式可跨样式和链接显示节点，代码与公式载荷及 URL 不参与扫描。"""
    spans = [
        {"type": "text", "content": "前Ａ \\"},
        {"type": "text", "content": "(Ｆ", "styles": ["superscript"]},
        {
            "type": "hyperlink",
            "url": "https://example.test/Ａ?q=１",
            "content": [
                {"type": "text", "content": "２\\) 标Ｂ", "styles": ["bold"]},
                {"type": "code_inline", "content": r"Ｃ\(Ｄ"},
            ],
        },
        {"type": "equation_inline", "content": r"Ｅ\)１"},
        {"type": "text", "content": " 后Ｆ"},
        {"type": "text", "content": "Ｇ", "styles": ["subscript"]},
    ]
    model = [[{"type": "caption", "content": spans, "lines": [{"bbox": [0.1, 0.1, 0.9, 0.2]}]}]]
    expected = deepcopy(model)
    target = expected[0][0]["content"]
    target[0]["content"] = "前A \\"
    target[2]["content"][0]["content"] = "２\\) 标B"
    target[4]["content"], target[5]["content"] = " 后F", "G"
    identities = [id(span) for span in spans]
    normalize_pdf_model_text(model)
    assert model == expected
    assert model[0][0]["content"] is spans
    assert [id(span) for span in spans] == identities
    normalize_pdf_model_text(model)
    assert model == expected


@pytest.mark.parametrize(
    "kind", [BlockType.CODE, RAW_ALGORITHM, BlockType.EQUATION, BlockType.IMAGE, BlockType.CHART, "unknown"]
)
@pytest.mark.parametrize("content", ["Ａｚ０，。", [{"type": "text", "content": "Ａｚ０，。"}]])
def test_non_natural_language_payloads_are_untouched(kind: str, content: Any) -> None:
    """代码与算法无论采用字符串或 Span 载荷，都保留其原文。"""
    model = [[{"type": kind, "content": deepcopy(content)}]]
    expected = deepcopy(model)
    normalize_pdf_model_text(model)
    assert model == expected


def test_table_converts_text_entities_and_keeps_structure() -> None:
    """覆盖实体、合并单元格、上下标、链接、图像和嵌套表格，属性值全部保持原样。"""
    markup = """<table data-name="Ａ"><thead><tr><th colspan="2">&#xFF21;&#65313;０，。</th></tr></thead>
<tbody><tr><td rowspan="2">Ｂ<sup>２</sup><sub>３</sub><a href="/Ａ?q=１">Ｃ</a><img src="images/Ｄ.png" alt="Ｅ"></td>
<td>Ｆ<table><tr><td>Ｇ</td></tr></table>Ｈ</td></tr><tr><td>Ｉ　①㎏</td></tr></tbody></table>"""
    model = [[{"type": "table", "content": markup}]]
    before = BeautifulSoup(markup, "html.parser")
    normalize_pdf_model_text(model)
    after = BeautifulSoup(model[0][0]["content"], "html.parser")
    assert [(tag.name, tag.attrs) for tag in after.find_all(True)] == [(tag.name, tag.attrs) for tag in before.find_all(True)]
    assert after.get_text() == before.get_text().translate(str.maketrans("ＡＢＣＤＥＦＧＨＩ０２３", "ABCDEFGHI023"))
    normalized = model[0][0]["content"]
    normalize_pdf_model_text(model)
    assert model[0][0]["content"] == normalized


def test_table_formula_delimiters_cross_html_nodes() -> None:
    """公式开始及结束标记可跨 span/a；未闭合保护止于当前单元格，不能污染下一格。"""
    markup = r"""<table><tr><td>Ａ\<span>(Ｆ</span><a href="/Ｂ">２\</a>)Ｃ<sup>３</sup></td>
<td>Ｄ\(<b>Ｅ</b>Ｆ</td><td>Ｇ</td></tr></table>"""
    model = [[{"type": "table", "content": markup}]]
    normalize_pdf_model_text(model)
    cells = BeautifulSoup(model[0][0]["content"], "html.parser").find_all("td")
    assert [cell.get_text() for cell in cells] == [r"A\(Ｆ２\)C3", r"D\(ＥＦ", "G"]
    assert cells[0].a["href"] == "/Ｂ"


def test_table_opaque_nodes_and_unchanged_markup_remain_intact() -> None:
    """仅含受保护载荷或属性时，不重新序列化 HTML；普通标签不能侵入公式与代码。"""
    markup = """<table data-name='Ａ'><tr><td><!-- Ｂ --><eq>Ｃ１</eq><math><mi>Ｄ</mi></math>
<code>Ｅ２</code><pre>Ｆ</pre><span data-docgale-latex='Ｇ'>Ｇ</span>
<span class='docgale-math'>Ｈ</span><div data-block-type='algorithm'>Ｉ</div>
<svg><text>Ｊ</text></svg><a href='/Ｋ'>ascii</a><img alt='Ｌ' src='/Ｍ'></td></tr></table>"""
    model = [[{"type": "table", "content": markup}]]
    normalize_pdf_model_text(model)
    assert model[0][0]["content"] == markup
    model[0][0]["content"] = markup.replace("ascii", "Ｎ")
    normalize_pdf_model_text(model)
    table = BeautifulSoup(model[0][0]["content"], "html.parser")
    assert table.a.get_text() == "N" and table.eq.get_text() == "Ｃ１"
    assert table.code.get_text() == "Ｅ２" and table.svg.get_text() == "Ｊ"


def test_numeric_entities_are_normalized_without_literal_fullwidth_characters() -> None:
    """纯 ASCII 编码的 HTML 字符实体同样需要转换。"""
    model = [[{"type": "table", "content": "<table><tr><td>&#65313;&#xFF11;</td></tr></table>"}]]
    normalize_pdf_model_text(model)
    assert BeautifulSoup(model[0][0]["content"], "html.parser").td.get_text() == "A1"
