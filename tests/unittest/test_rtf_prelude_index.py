"""RTF 前导组索引的工作量、异常顺序与独立词法预算回归。"""

from __future__ import annotations

from unittest.mock import Mock, call

import pytest

from docvortex.analyzers.native.office.errors import LegacyOfficeMalformedError, LegacyOfficeResourceLimitError
from docvortex.analyzers.native.office.rtf import lexer as lexer_module
from docvortex.analyzers.native.office.rtf import parser as parser_module
from docvortex.analyzers.native.office.rtf.lexer import RtfLexer

_SOURCE = (
    rb"{\rtf1\ansi"
    rb"{\fonttbl{\f0\fcharset204 Arial;}{\f1\fcharset128 MS Gothic;}}"
    rb"{\stylesheet{\s0 Normal;}{\s1\sbasedon0\b Heading 1;}}"
    rb"{\*\listtable{\list{\listlevel\levelnfc0\levelstartat3}\listid7}}"
    rb"{\*\listoverridetable{\listoverride\listid7\listoverridecount0\ls2}}"
    rb"{\info{\title Header}{\author Author}{\subject Subject}{\keywords Alpha}}"
    rb"\pard\s1 Body\par}"
)


def test_rtf_prelude_scans_the_complete_source_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证字体、样式、列表和属性共用一次全文词法扫描，嵌套组仍独立解析。"""
    inputs: list[bytes] = []

    def tracking_lexer(data: bytes) -> RtfLexer:
        """记录每次扫描的输入范围，同时使用原 lexer 执行所有边界校验。"""
        inputs.append(data)
        return RtfLexer(data)

    monkeypatch.setattr(parser_module, "RtfLexer", tracking_lexer)

    prelude = parser_module.parse_rtf_prelude(_SOURCE)

    assert sum(data == _SOURCE for data in inputs) == 1
    assert prelude.font_encodings == {0: "cp1251", 1: "cp932"}
    assert prelude.styles[1].outline_level == 0
    assert prelude.styles[1].text_style.bold
    assert prelude.lists[2].levels[0].start == 3
    assert prelude.metadata.title == "Header"
    assert prelude.metadata.author == "Author"
    assert prelude.metadata.subject == "Subject"
    assert prelude.metadata.keywords == "Alpha"


def test_rtf_prelude_keeps_per_scan_token_limits_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证预算仍限制单次 lexer，而不把全文和字体等嵌套扫描累计为一个额度。"""
    token_count = len(list(RtfLexer(_SOURCE)))
    monkeypatch.setattr(lexer_module, "MAX_RECORDS", token_count)

    assert parser_module.parse_rtf_prelude(_SOURCE).metadata.title == "Header"

    monkeypatch.setattr(lexer_module, "MAX_RECORDS", token_count - 1)
    with pytest.raises(LegacyOfficeResourceLimitError, match=f"max_tokens={token_count - 1}"):
        parser_module.parse_rtf_prelude(_SOURCE)


def test_rtf_prelude_binary_payload_cannot_create_header_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 bin 载荷中的伪字体和属性组不会进入索引或产生代码页告警。"""
    warning = Mock()
    monkeypatch.setattr(parser_module.logger, "warning", warning)
    payload = rb"{\fonttbl{\f0\cpg99998 Fake;}}{\info{\title Fake}}"
    source = rb"{\rtf1\bin" + str(len(payload)).encode("ascii") + b" " + payload + rb"{\info{\title Real}}}"

    prelude = parser_module.parse_rtf_prelude(source)

    assert prelude.font_encodings == {}
    assert prelude.metadata.title == "Real"
    warning.assert_not_called()


def test_rtf_prelude_keeps_nested_first_match_in_group_closing_order() -> None:
    """验证重复 info 仍按既有闭合顺序取首个组，不改成最外层或源码起点顺序。"""
    source = rb"{\rtf1{\info{\title Outer}{\info{\title Inner}}}{\info{\title Later}}}"

    assert parser_module.parse_rtf_prelude(source).metadata.title == "Inner"


@pytest.mark.parametrize(
    ("source", "expected_title"),
    [
        (rb"{\rtf1{\info{\title Kept}} tail", "Kept"),
        (rb"{\rtf1{\info{\title Incomplete} tail", None),
        (b"\xef\xbb\xbf  " + rb"{\rtf1{\info{\title Prefix}}}", "Prefix"),
    ],
)
def test_rtf_prelude_preserves_unclosed_group_and_header_prefix_semantics(source: bytes, expected_title: str | None) -> None:
    """验证未闭合根组、未闭合属性组和允许的 BOM 前缀仍得到相同属性。"""
    assert parser_module.parse_rtf_prelude(source).metadata.title == expected_title


def test_rtf_prelude_preserves_default_then_first_font_warning_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证默认编码先告警，随后仅解析首个字体表，不额外解释重复定义。"""
    warning = Mock()
    monkeypatch.setattr(parser_module.logger, "warning", warning)
    source = (
        rb"{\rtf1\ansicpg99999"
        rb"{\fonttbl{\f0\cpg99998 First;}}"
        rb"{\fonttbl{\f0\cpg99997 Ignored;}}Text}"
    )

    prelude = parser_module.parse_rtf_prelude(source)

    assert prelude.font_encodings == {0: "cp1252"}
    assert warning.call_args_list == [
        call("Unsupported RTF code page {}, falling back to {}", 99999, "cp1252"),
        call("Unsupported RTF code page {}, falling back to {}", 99998, "cp1252"),
    ]


@pytest.mark.parametrize(
    ("tail", "error", "message"),
    [
        (rb"\bin20 x}", LegacyOfficeMalformedError, "bin payload is truncated"),
        (rb"\foo2147483648}", LegacyOfficeResourceLimitError, "signed 32-bit"),
        (b"{" * 256 + b"}" * 257, LegacyOfficeResourceLimitError, "max_group_depth"),
    ],
)
def test_rtf_prelude_checks_the_whole_stream_before_font_interpretation(
    monkeypatch: pytest.MonkeyPatch,
    tail: bytes,
    error: type[Exception],
    message: str,
) -> None:
    """验证尾部词法错误仍先于字体解析，且默认代码页告警的既有优先序不变。"""
    warning = Mock()
    monkeypatch.setattr(parser_module.logger, "warning", warning)
    source = rb"{\rtf1\ansicpg99999{\fonttbl{\f0\cpg99998 First;}}" + tail

    with pytest.raises(error, match=message):
        parser_module.parse_rtf_prelude(source)

    warning.assert_called_once_with("Unsupported RTF code page {}, falling back to {}", 99999, "cp1252")


def test_rtf_prelude_indexes_do_not_cross_document_boundaries() -> None:
    """验证组索引仅在单次 prelude 调用内存活，下一文档不会复用前份定义。"""
    first = parser_module.parse_rtf_prelude(_SOURCE)
    second = parser_module.parse_rtf_prelude(rb"{\rtf1{\info{\title Second}}}")

    assert first.metadata.title == "Header"
    assert second.metadata.title == "Second"
    assert second.font_encodings == {}
    assert second.styles == {}
    assert second.lists == {}
