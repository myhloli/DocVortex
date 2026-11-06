"""跨页单元格的自然语言边界、HTML 保真和公开渲染回归。"""

from __future__ import annotations

from copy import deepcopy
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest
from bs4 import BeautifulSoup, Tag

from docvortex.content.table import merge_table_content
from docvortex.content.table.content import _append_cell_content
from docvortex.render import (
    RenderMode,
    render_docx,
    render_epub,
    render_html,
    render_latex,
    render_markdown,
    render_pdf,
    render_structured_content,
)
from docvortex.schema import MiddleJson


def _table(content: str, *, continued: bool = False) -> dict[str, Any]:
    """构造单列续接表格，让测试经过真实网格和内容合并入口。"""
    table: dict[str, Any] = {
        "type": "table",
        "index": 0,
        "bbox": [0.1, 0.1, 0.9, 0.9],
        "content": [
            {
                "type": "table_body",
                "index": 0,
                "bbox": [0.1, 0.1, 0.9, 0.9],
                "content": f"<table><tr><td>{content}</td></tr></table>",
            }
        ],
    }
    if continued:
        table.update(continues_prev=True, cell_merge=[1])
    return table


def _merged_cell(previous: str, current: str) -> Tag:
    """合并两格并验证源对象不变，返回结果中的第一格供文字与结构断言。"""
    tables = (_table(previous), _table(current, continued=True))
    original = deepcopy(tables)
    merged = merge_table_content(*tables)
    assert merged is not None
    assert tables == original
    cell = BeautifulSoup(merged["content"][0]["content"], "html.parser").find("td")
    assert isinstance(cell, Tag)
    return cell


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        ("in", "force", "in force"),
        ("A", "B", "A B"),
        ("中文", "续接", "中文续接"),
        ("中文 English", "words", "中文 English words"),
        ("English", "中文", "English中文"),
        ("日本語", "の文章", "日本語の文章"),
        ("한국어", "문장", "한국어 문장"),
        ("𠀀", "𰀀", "𠀀𰀀"),
        ("か\u3099", "文字", "か\u3099文字"),
        ("cafe\u0301", "words", "cafe\u0301 words"),
        ("inter-", "national", "international"),
        ("inter\u00ad", "national", "international"),
        ("inter\u2010", "national", "international"),
        ("inter", "national", "inter national"),
        ("Open-", "domain", "Open-domain"),
        ("GLGE-", "difficult", "GLGE-difficult"),
        ("chain-of-", "thought", "chain-of-thought"),
        ("word", ", next", "word, next"),
        ("word", ")", "word)"),
        ("(", "word", "(word"),
        ("123", "456", "123 456"),
        ("11-", "20", "11-20"),
        ("11–", "20", "11–20"),
        ("in ", "force", "in force"),
        ("in", " force", "in force"),
        ("in  ", " force", "in   force"),
        ("in&nbsp;", "force", "in\u00a0force"),
        ("in", "&#160;force", "in\u00a0force"),
        ("in\t", "force", "in\tforce"),
        ("in", "\nforce", "in\nforce"),
        ("inter- ", "national", "inter- national"),
        ("保留  内部", "空格", "保留  内部空格"),
        ("See https://example.test/current", "/path", "See https://example.test/current/path"),
        ("DOI https", "://doi.org/example", "DOI https://doi.org/example"),
        ("https://example.test/a", "https://example.test/b", "https://example.test/a https://example.test/b"),
        ("", "next", "next"),
        ("<span></span>", "next", "next"),
    ],
)
def test_cell_merge_uses_text_boundary_rules(previous: str, current: str, expected: str) -> None:
    """使用固定期望覆盖多语言、断词、URL 以及已有空白，不调用被测规则生成答案。"""
    assert _merged_cell(previous, current).get_text() == expected


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        ("<b>in</b>", "<span>force</span>", "<b>in</b> <span>force</span>"),
        (
            '<b>inter</b><span data-id="hyphen">-</span><!--tail-->',
            '<!--head--><a href="https://example.test/a-b" title="keep">national</a>',
            '<b>inter</b><span data-id="hyphen"></span><!--tail-->'
            '<!--head--><a href="https://example.test/a-b" title="keep">national</a>',
        ),
        ("<b>in</b><!--tail--><span></span>", "<!--head-->force", "<b>in</b><!--tail--><span></span> <!--head-->force"),
        ("<b>in</b><span>&nbsp;</span>", "<i>force</i>", "<b>in</b><span>&nbsp;</span><i>force</i>"),
        (
            '<span class="word" style="color:red">Open</span><i>-</i>',
            '<a href="https://example.test/">domain</a>',
            '<span class="word" style="color:red">Open</span><i>-</i><a href="https://example.test/">domain</a>',
        ),
        ("chain-<b>of</b><i>-</i>", "<u>thought</u>", "chain-<b>of</b><i>-</i><u>thought</u>"),
        ("See https://<b>example.</b>", "<i>org/path</i>", "See https://<b>example.</b><i>org/path</i>"),
        ("<code>x-y</code>in", "force<eq>x-y</eq>", "<code>x-y</code>in force<eq>x-y</eq>"),
    ],
)
def test_cell_merge_preserves_inline_nodes(previous: str, current: str, expected: str) -> None:
    """边界字符可跨样式叶子，修改仍仅限断词符及独立空格，属性和注释原样保留。"""
    cell = _merged_cell(previous, current)
    assert str(cell) == str(BeautifulSoup(f"<td>{expected}</td>", "html.parser").td)


@pytest.mark.parametrize("at_target", [True, False])
@pytest.mark.parametrize(
    "barrier",
    [
        "<br>",
        "<hr>",
        "<p>paragraph</p>",
        "<div>block</div>",
        '<img src="images/one.png" alt="keep">',
        "<eq>x-y</eq>",
        "<math><mi>x</mi></math>",
        "<code>foo-</code>",
        "<pre>foo  bar</pre>",
        "<svg><text>x</text></svg>",
        '<span data-docvortex-latex="x-y">formula</span>',
        '<span data-formula-display="inline">formula</span>',
        '<span class="docvortex-math">formula</span>',
        '<span data-block-type="code">foo-</span>',
        '<span><em data-block-type="algorithm">foo-</em></span>',
        "<custom>unknown</custom>",
    ],
)
def test_cell_merge_does_not_cross_structural_boundaries(barrier: str, at_target: bool) -> None:
    """两侧遇到结构或不透明载荷时直接迁移，不能跨越它们取字或改写内部内容。"""
    previous, current = ("in" + barrier, "force") if at_target else ("in", barrier + "force")
    cell = _merged_cell(previous, current)
    assert str(cell) == str(BeautifulSoup(f"<td>{previous}{current}</td>", "html.parser").td)


def test_empty_source_cell_with_merge_flag_is_consumed() -> None:
    """显式续接标记同样连接空白区域，不向目标添加额外文字。"""
    merged = merge_table_content(_table("in"), _table("<span></span>", continued=True))
    assert merged is not None
    soup = BeautifulSoup(merged["content"][0]["content"], "html.parser")
    assert [cell.get_text() for cell in soup.find_all("td")] == ["in"]


@pytest.mark.parametrize("at_target", [True, False])
def test_cell_content_transfer_preserves_nested_table_boundary(at_target: bool) -> None:
    """单独检查嵌套表格的内容转移屏障，不要求外层网格算法支持嵌套表格。"""
    nested = '<table data-id="nested"><tr><td>inter-</td></tr></table>'
    previous, current = ("in" + nested, "force") if at_target else ("in", nested + "force")
    target = BeautifulSoup(f"<td>{previous}</td>", "html.parser").td
    source = BeautifulSoup(f"<td>{current}</td>", "html.parser").td
    assert isinstance(target, Tag) and isinstance(source, Tag)
    _append_cell_content(target, source)
    assert str(target) == str(BeautifulSoup(f"<td>{previous}{current}</td>", "html.parser").td)
    assert not source.contents


@pytest.mark.parametrize("at_target", [True, False])
def test_cell_content_transfer_preserves_opaque_cell_payload(at_target: bool) -> None:
    """公式或代码标记位于单元格本身时，也不能修改其中的文字载荷。"""
    target = BeautifulSoup('<td data-block-type="code">inter-</td>' if at_target else "<td>inter-</td>", "html.parser").td
    source = BeautifulSoup("<td>national</td>" if at_target else '<td data-block-type="code">national</td>', "html.parser").td
    assert isinstance(target, Tag) and isinstance(source, Tag)
    _append_cell_content(target, source)
    assert target.get_text() == "inter-national"
    assert not source.contents


def test_cell_merge_keeps_url_context_across_three_pages() -> None:
    """连续三页合并使用累计行内上下文，不能把 URL 的后续路径当成普通词间空格。"""
    first = _table("See https://<b>example.</b>")
    second = _table("<i>org/path/</i>", continued=True)
    third = _table("<u>file</u>", continued=True)
    merged = merge_table_content(first, second)
    assert merged is not None
    merged = merge_table_content(merged, third)
    assert merged is not None
    soup = BeautifulSoup(merged["content"][0]["content"], "html.parser")
    assert soup.get_text() == "See https://example.org/path/file"
    assert all(soup.find(tag) is not None for tag in ("b", "i", "u"))


def test_live_enhancement_fixture_renders_correct_boundary_without_mutating_source() -> None:
    """重放两站实调时的合成表格，验证部分列续接、公开导出与原始分页协议。"""
    fixture = Path(__file__).parents[1] / "fixtures" / "table_merge_english_boundary.json"
    middle = MiddleJson.model_validate_json(fixture.read_text(encoding="utf-8"))
    original = middle.model_dump_json()
    markdown = render_markdown(middle)
    assert '<td rowspan="2">The agreement remains in force until December 2027.</td>' in markdown
    assert "<tr><td>200</td></tr>" in markdown
    html = render_html(middle, standalone=False)
    soup = BeautifulSoup(html, "html.parser")
    assert len(soup.find_all("table")) == 1
    assert [[cell.get_text() for cell in row.find_all(["td", "th"])] for row in soup.find_all("tr")] == [
        ["Terms", "Amount"],
        ["The agreement remains in force until December 2027.", "100"],
        ["200"],
    ]
    assert soup.find_all("tr")[1].td["rowspan"] == "2"
    assert "inforce" not in markdown + html
    assert "| The agreement remains in | 100 |" in render_markdown(middle, mode=RenderMode.FULL)
    assert len(BeautifulSoup(render_html(middle, mode=RenderMode.FULL), "html.parser").find_all("table")) == 2
    structured = render_structured_content(middle)
    assert "| The agreement remains in | 100 |" in structured["pages"][0]["blocks"][0]["content"]
    assert structured["pages"][1]["blocks"][0]["cell_merge"] == [1, 0]
    assert "| force until December 2027. | 200 |" in structured["pages"][1]["blocks"][0]["content"]
    assert middle.model_dump_json() == original


@pytest.mark.parametrize("format_name", ["docx", "pdf", "epub", "latex"])
def test_cell_boundary_is_preserved_in_exported_artifact(format_name: str) -> None:
    """读取各格式真实导出载荷，确认修复经过共用规划器进入文档且不污染输入。"""
    fixture = Path(__file__).parents[1] / "fixtures" / "table_merge_english_boundary.json"
    middle = MiddleJson.model_validate_json(fixture.read_text(encoding="utf-8"))
    original = middle.model_dump_json()
    if format_name == "docx":
        with ZipFile(BytesIO(render_docx(middle))) as archive:
            document = ElementTree.fromstring(archive.read("word/document.xml"))
        text = "".join(document.itertext())
    elif format_name == "pdf":
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(render_pdf(middle)))
        text = " ".join(page.extract_text() for page in reader.pages)
    elif format_name == "epub":
        with ZipFile(BytesIO(render_epub(middle))) as archive:
            text = " ".join(
                "".join(ElementTree.fromstring(archive.read(name)).itertext())
                for name in archive.namelist()
                if name.endswith(".xhtml")
            )
    else:
        text = render_latex(middle)
    assert "The agreement remains in force until December 2027." in " ".join(text.split())
    assert "inforce" not in text
    assert "100" in text and "200" in text
    assert middle.model_dump_json() == original
