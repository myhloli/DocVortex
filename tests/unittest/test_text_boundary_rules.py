"""验证无模型依赖的物理行边界规则及结构化 Span 保真。"""

from copy import deepcopy

import pytest

from docvortex.content.inline import inline_plain_text, join_inline_spans
from docvortex.foundation._text import merge_text_line_contents
from docvortex.schema import CodeInlineSpan, EquationInlineSpan, HyperlinkSpan, TextSpan


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (["中文 English", "words"], "中文 English words"),
        (["English", "中文"], "English中文"),
        (["日本語", "の文章"], "日本語の文章"),
        (["한국어", "문장"], "한국어 문장"),
        (["𠀀", "𰀀"], "𠀀𰀀"),
        (["か\u3099", "文字"], "か\u3099文字"),
        (["cafe\u0301", "words"], "cafe\u0301 words"),
        (["word", ")"], "word)"),
        (["(", "word"], "(word"),
        (["word", ", next"], "word, next"),
        (["中文，", "English"], "中文，English"),
        (["123", "456"], "123 456"),
        (["保留  内部", "空格"], "保留  内部空格"),
        (["中文 inter-", "national"], "中文 international"),
        (["中文 GLGE-", "difficult"], "中文 GLGE-difficult"),
        (["Open-", "domain"], "Open-domain"),
        (["chain-of-", "thought"], "chain-of-thought"),
        (["11-", "20"], "11-20"),
        (["Boost-Deci-", "sion"], "Boost-Decision"),
        (["cross-", "Platform"], "cross-Platform"),
        (["em—", "dash"], "em— dash"),
        (["a<sup>2</sup>", "times"], "a<sup>2</sup> times"),
        (["", " \t", "\ud800中文", "\udfff继续"], "中文继续"),
        (["", "\ud800", " \t"], ""),
        (["https://example.", "org/path/", "file"], "https://example.org/path/file"),
        (["https://example.org/a", "https://example.org/b"], "https://example.org/a https://example.org/b"),
    ],
)
def test_boundary_rules(lines: list[str], expected: str) -> None:
    """仅当前边界参与空格决策，保留内部内容以及已有 URL 和断词规则。"""
    assert merge_text_line_contents(lines) == expected


def test_inline_boundaries_preserve_payloads_and_source() -> None:
    """公式、代码、链接目标及样式原样保留，断词只删除末尾文字叶子的字符。"""
    contents = [
        [TextSpan(type="text", content="中文 inter-", styles=["bold"])],
        [HyperlinkSpan(type="hyperlink", url="https://example.org/a-b", content=[TextSpan(type="text", content="national")])],
        [EquationInlineSpan(type="equation_inline", content="x-y")],
        [CodeInlineSpan(type="code_inline", content="foo-bar")],
        [TextSpan(type="text", content="next")],
    ]
    original = deepcopy(contents)
    joined = join_inline_spans(contents)
    assert inline_plain_text(joined) == "中文 international x-y foo-bar next"
    assert joined[0].content == "中文 inter"
    assert joined[0].styles == ["bold"]
    assert any(isinstance(span, HyperlinkSpan) and span.url == "https://example.org/a-b" for span in joined)
    assert any(isinstance(span, EquationInlineSpan) and span.content == "x-y" for span in joined)
    assert any(isinstance(span, CodeInlineSpan) and span.content == "foo-bar" for span in joined)
    assert contents == original
