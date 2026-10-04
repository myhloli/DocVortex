"""Verify PDF output cleaning of text ranges, structure protection, and cross-node formula boundaries."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from bs4 import BeautifulSoup
import pytest

from docvortex.content import normalize_pdf_model_text
from docvortex.schema import BlockType, RAW_ALGORITHM, RAW_CAPTION, RAW_FOOTNOTE, RAW_PHONETIC


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
    """Migrate the old cleaning range, retaining punctuation, full-width spaces, circles, compatible units and combining characters."""
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
    """Existing formula delimiters and unclosed formulas are retained, and ordinary strings are not interpreted as HTML."""
    model = [[{"type": "text", "content": source}]]
    normalize_pdf_model_text(model)
    assert model[0][0]["content"] == expected


def test_span_boundaries_styles_and_urls_are_preserved() -> None:
    """Formulas can display nodes across styles and links, code and formula loads and URL are not involved in scanning."""
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
    """Code and algorithms retain their original text regardless of whether they use string or Span payloads."""
    model = [[{"type": kind, "content": deepcopy(content)}]]
    expected = deepcopy(model)
    normalize_pdf_model_text(model)
    assert model == expected


def test_table_converts_text_entities_and_keeps_structure() -> None:
    """Entities, merged cells, superscripts and subscripts, links, images, and nested tables are overwritten, and attribute values are all left intact."""
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
    """The start and end marks of the formula can span span/a; the unclosed protection is limited to the current cell and cannot contaminate the next cell."""
    markup = r"""<table><tr><td>Ａ\<span>(Ｆ</span><a href="/Ｂ">２\</a>)Ｃ<sup>３</sup></td>
<td>Ｄ\(<b>Ｅ</b>Ｆ</td><td>Ｇ</td></tr></table>"""
    model = [[{"type": "table", "content": markup}]]
    normalize_pdf_model_text(model)
    cells = BeautifulSoup(model[0][0]["content"], "html.parser").find_all("td")
    assert [cell.get_text() for cell in cells] == [r"A\(Ｆ２\)C3", r"D\(ＥＦ", "G"]
    assert cells[0].a["href"] == "/Ｂ"


def test_table_opaque_nodes_and_unchanged_markup_remain_intact() -> None:
    """HTML is not reserialized when it contains only protected payloads or attributes; ordinary tags cannot invade formulas and codes."""
    markup = """<table data-name='Ａ'><tr><td><!-- Ｂ --><eq>Ｃ１</eq><math><mi>Ｄ</mi></math>
<code>Ｅ２</code><pre>Ｆ</pre><span data-docvortex-latex='Ｇ'>Ｇ</span>
<span class='docvortex-math'>Ｈ</span><div data-block-type='algorithm'>Ｉ</div>
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
    """Pure ASCII encoded HTML character entities also need to be converted."""
    model = [[{"type": "table", "content": "<table><tr><td>&#65313;&#xFF11;</td></tr></table>"}]]
    normalize_pdf_model_text(model)
    assert BeautifulSoup(model[0][0]["content"], "html.parser").td.get_text() == "A1"
