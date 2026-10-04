"""Verify PDF symbol whitelisting and existing formula, code and structure protection."""

from __future__ import annotations

from copy import deepcopy

import pytest
from bs4 import BeautifulSoup

from docvortex.content import normalize_pdf_model_text
from docvortex.foundation._text import full_to_half_exclude_marks

_SYMBOLS = "：．／＼－＿％＋＝＠＃＆＊"
_ASCII = ":./\\-_%+=@#&*"


@pytest.mark.parametrize(("source", "target"), tuple(zip(_SYMBOLS, _ASCII)))
def test_symbol_only_text_and_table(source: str, target: str) -> None:
    """When there are no full-width alphanumeric characters, text and cells still perform symbol mapping for each item."""
    model = [[{"type": "text", "content": source}, {"type": "table", "content": f"<table><tr><td>{source}</td></tr></table>"}]]
    normalize_pdf_model_text(model)
    assert model[0][0]["content"] == target
    assert BeautifulSoup(model[0][1]["content"], "html.parser").td.get_text() == target
    expected = deepcopy(model)
    normalize_pdf_model_text(model)
    assert model == expected


def test_unlisted_punctuation_and_alphanumeric_helper_are_unchanged() -> None:
    """Chinese punctuation and independent Unicode symbols remain unchanged, and the general tool still only processes English numbers."""
    kept = "，、。；！？（）［］｛｝＜＞～＂＇｜｀“”‘’《》【】「」『』　 −–—·…×÷≤≥"
    model = [[{"type": "text", "content": _SYMBOLS + kept}]]
    normalize_pdf_model_text(model)
    assert model[0][0]["content"] == _ASCII + kept
    assert full_to_half_exclude_marks("Ａ１" + _SYMBOLS) == "A1" + _SYMBOLS


def test_symbols_preserve_spans_links_and_formula_boundaries() -> None:
    """Cross-node formulas are left intact, symbol mapping does not rebuild Span, does not change URL or code."""
    spans = [
        {"type": "text", "content": "值：Ａ／Ｂ \\"},
        {"type": "text", "content": "(Ｘ＋Ｙ", "styles": ["bold"]},
        {"type": "hyperlink", "url": "https://example.test/Ａ：Ｂ", "content": [{"type": "text", "content": "＝Ｚ\\) ５０％"}]},
        {"type": "code_inline", "content": _SYMBOLS},
        {"type": "equation_inline", "content": _SYMBOLS},
        {"type": "text", "content": "＼(Ａ＋Ｂ＼)"},
    ]
    model = [[{"type": "text", "content": spans}]]
    expected = deepcopy(model)
    expected[0][0]["content"][0]["content"] = "值:A/B \\"
    expected[0][0]["content"][2]["content"][0]["content"] = "＝Ｚ\\) 50%"
    expected[0][0]["content"][5]["content"] = "\\(A+B\\)"
    identities = [id(span) for span in spans]
    normalize_pdf_model_text(model)
    assert model == expected
    assert [id(span) for span in spans] == identities
    normalize_pdf_model_text(model)
    assert model == expected


def test_table_entities_attributes_and_opaque_nodes_are_preserved() -> None:
    """HTML Entities and cross-label text will be converted, but attributes, formulas, codes, and image loads remain unchanged."""
    markup = r"""<table data-note="："><tr><td colspan="2"><b>&#xff1a;</b><sup>％</sup>
<a href="https://example.test/：">／</a><img src="images/：.png">
<span>\(Ａ</span><b>＋Ｂ\)</b><eq>Ａ＋Ｂ</eq><code>：／</code>
<span data-docvortex-latex="Ａ＋Ｂ">Ａ＋Ｂ</span></td></tr></table>"""
    model = [[{"type": "table", "content": markup}]]
    normalize_pdf_model_text(model)
    actual = BeautifulSoup(model[0][0]["content"], "html.parser")
    original = BeautifulSoup(markup, "html.parser")
    assert [node.attrs for node in actual.find_all()] == [node.attrs for node in original.find_all()]
    assert actual.b.get_text() == ":" and actual.sup.get_text() == "%" and actual.a.get_text() == "/"
    assert actual.eq.get_text() == "Ａ＋Ｂ" and actual.code.get_text() == "：／"
    assert actual.select_one("[data-docvortex-latex]").get_text() == "Ａ＋Ｂ"
    assert r"\(Ａ＋Ｂ\)" in actual.get_text()
    expected = deepcopy(model)
    normalize_pdf_model_text(model)
    assert model == expected


def test_unclosed_formula_and_non_natural_blocks_remain_opaque() -> None:
    """Unclosed formulas are protected to the end of the paragraph, and formulas, codes and algorithm blocks do not participate in symbol mapping."""
    model = [
        [
            {"type": "text", "content": "： \\(Ａ＋Ｂ："},
            *[{"type": kind, "content": _SYMBOLS} for kind in ["equation", "code", "algorithm"]],
        ]
    ]
    expected = deepcopy(model)
    expected[0][0]["content"] = ": \\(Ａ＋Ｂ："
    normalize_pdf_model_text(model)
    assert model == expected
