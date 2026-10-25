"""RTF 批量组扫描的冻结差分、错误优先序和公开入口回归。"""

from __future__ import annotations

import hashlib
from pathlib import Path
import random
from unittest.mock import Mock

import pytest

import _rtf_groups_oracle as oracle
from docvortex import parse
from docvortex._compute_backend import get_native
from docvortex.analyzers.native.office.errors import LegacyOfficeMalformedError, LegacyOfficeResourceLimitError
from docvortex.analyzers.native.office.rtf import lexer, parser

ROOT = Path(__file__).resolve().parents[1]


def _expected(data: bytes, direct: bool, budget: int, depth: int) -> tuple:
    """独立执行冻结词法合同，同时保留完整异常分类和消息。"""
    oracle.MAX_RECORDS = budget
    oracle.MAX_RTF_GROUP_DEPTH = depth
    try:
        return oracle.group_spans(data, direct), None
    except (LegacyOfficeMalformedError, LegacyOfficeResourceLimitError) as error:
        return [], (isinstance(error, LegacyOfficeResourceLimitError), str(error))


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"}}}{{a{b}}{c}}{second}",
        b"{{unclosed",
        rb"{\rtf1\bin5 }}{\x {after}}",
        rb"{\RTF1\BIN0 {after}}",
        rb"{\bin-0{after}}",
        rb"{\bin-1 x}",
        rb"{\bin- x}",
        rb"{\bin x}",
        rb"{\bin10 x}",
        rb"{\foo2147483647\bar-2147483648{ok}}",
        rb"{\foo2147483648}",
        rb"{\foo-2147483649}",
        rb"{\foo00000000000}",
        rb"{\bin99999999999 x}",
        b"{\\\r\n{a}\\\n{b}\\\r{c}\\}",
        b"{\\'f {a}\\'+f{b}\\'-f{c}\\'  {d}\\'\xfff{e}}",
        b"{\\'\\{not-escaped}}",
        b"{\\'f",
        b"\\",
        b"{" * 257 + b"}" * 257,
        rb"{\rtf1\bin4 }{\x{\foo2147483648}}",
    ],
)
def test_native_group_scan_directed(data: bytes, direct: bool) -> None:
    """定向覆盖二进制、参数、转义、不完整输入和错误优先级，并逐一比较预算边界。"""
    for budget in [0, 1, 3, 20, 16_000_000]:
        for depth in [0, 1, 256]:
            assert get_native().rtf_group_spans(data, direct, budget, depth) == _expected(data, direct, budget, depth)


@pytest.mark.parametrize("seed", range(128))
def test_native_group_scan_generated(seed: int) -> None:
    """固定种子组合原始字节和控制流，独立比较两种组选择及多档预算下的结果。"""
    rng = random.Random(seed)
    parts = [
        b"{",
        b"}",
        b"text\r\n",
        b"\\",
        b"\\\r\n",
        b"\\\xff",
        b"\\'",
        rb"\bin0 ",
        rb"\bin-0 ",
        rb"\bin4 }{\x",
        rb"\bin-1 ",
        rb"\BiN9 ",
        rb"\rtf1",
        rb"\foo-",
        rb"\foo+1",
        rb"\u-2147483648",
        rb"\foo2147483648",
        rb"\foo00000000000",
        rb"\foo9999999999",
        rb"\foo0000000000",
        rb"\'ff",
    ]
    source = b"{\\rtf1" + b"".join(
        rng.choice(parts) if rng.randrange(3) else rng.randbytes(rng.randrange(12)) for _ in range(90)
    )
    for direct in [False, True]:
        for budget, depth in [(0, 0), (5, 3), (5000, 256)]:
            assert get_native().rtf_group_spans(source, direct, budget, depth) == _expected(source, direct, budget, depth)


def test_native_group_scan_all_two_byte_hex_candidates() -> None:
    """穷举全部两个字节的十六进制候选，用恰好一个 token 的预算暴露消费范围差异。"""
    native = get_native()
    for first in range(256):
        for second in range(256):
            source = b"\\'" + bytes([first, second])
            assert native.rtf_group_spans(source, False, 1, 256) == _expected(source, False, 1, 256), source


@pytest.mark.parametrize("budget,depth", [(0, 256), (3, 256), (100, 1), (100, 0)])
def test_python_adapter_preserves_changed_resource_limits(monkeypatch: pytest.MonkeyPatch, budget: int, depth: int) -> None:
    """动态词法预算直接传入原生扫描，异常类型与错误消息仍与冻结生成器相同。"""
    source = b"{{a}{b}}"
    monkeypatch.setattr(lexer, "MAX_RECORDS", budget)
    monkeypatch.setattr(lexer, "MAX_RTF_GROUP_DEPTH", depth)
    expected = _expected(source, False, budget, depth)
    assert expected[1] is not None
    with pytest.raises(LegacyOfficeResourceLimitError) as caught:
        parser._group_spans(source)
    assert str(caught.value) == expected[1][1]


@pytest.mark.parametrize(
    "override",
    [
        "factory",
        "iterator",
        "initializer",
        "letter",
        "digit",
        "digits",
        "minimum",
        "maximum",
        "budget-type",
        "depth-negative",
        "token-type",
        "parser-token-type",
        "bytes-subclass",
        "direct-type",
    ],
)
def test_python_adapter_retains_dynamic_lexer_contract(monkeypatch: pytest.MonkeyPatch, override: str) -> None:
    """替换词法工厂、方法、助手或参数合同后继续使用原迭代路径，不静默跳过定制逻辑。"""
    source = rb"{\rtf1{\info{\title Kept}}body}"
    direct = False
    if override == "factory":
        monkeypatch.setattr(parser, "RtfLexer", Mock(wraps=lexer.RtfLexer))
    elif override == "iterator":
        original = lexer.RtfLexer.__iter__

        def iterate(self):
            """保持 token 结果，标记自定义迭代入口以验证原生快路绕行。"""
            return original(self)

        monkeypatch.setattr(lexer.RtfLexer, "__iter__", iterate)
    elif override == "initializer":
        original_init = lexer.RtfLexer.__init__

        def initialize(self, data):
            """保持初始化结果，验证定制构造方法仍会执行。"""
            original_init(self, data)

        monkeypatch.setattr(lexer.RtfLexer, "__init__", initialize)
    elif override in {"letter", "digit"}:
        name = "_is_ascii_" + override
        monkeypatch.setattr(lexer, name, Mock(wraps=getattr(lexer, name)))
    elif override == "digits":
        monkeypatch.setattr(lexer, "MAX_RTF_CONTROL_PARAMETER_DIGITS", 11)
    elif override == "minimum":
        monkeypatch.setattr(lexer, "MIN_RTF_CONTROL_PARAMETER", -(2**40))
    elif override == "maximum":
        monkeypatch.setattr(lexer, "MAX_RTF_CONTROL_PARAMETER", 2**40)
    elif override == "budget-type":
        monkeypatch.setattr(lexer, "MAX_RECORDS", 16_000_000.0)
    elif override == "depth-negative":
        monkeypatch.setattr(lexer, "MAX_RTF_GROUP_DEPTH", -1)
    elif override == "token-type":
        monkeypatch.setattr(lexer, "RtfOpen", oracle.RtfOpen)
    elif override == "parser-token-type":
        monkeypatch.setattr(parser, "RtfOpen", oracle.RtfOpen)
    elif override == "bytes-subclass":

        class CustomBytes(bytes):
            """模拟自定义字节输入，保留原 lexer 对其访问行为。"""

        source = CustomBytes(source)
    elif override == "direct-type":
        direct = 1
    native = Mock(side_effect=AssertionError("unexpected native scan"))
    monkeypatch.setattr(get_native(), "rtf_group_spans", native)
    if override == "depth-negative":
        with pytest.raises(LegacyOfficeResourceLimitError):
            parser._group_spans(source, direct_only=direct)
    else:
        parser._group_spans(source, direct_only=direct)
    native.assert_not_called()


def _full_output(result) -> tuple:
    """完整比较两份公开结构、素材及诊断，不忽略坐标、顺序和 producer。"""
    return (
        result.model_json.model_dump(mode="json"),
        result.middle_json.model_dump(mode="json"),
        {name: (asset if isinstance(asset, bytes) else bytes(asset)) for name, asset in result.assets.items()},
        result.diagnostics,
    )


@pytest.mark.parametrize("relative", ["demo/ms_office_docs/rtf_01.rtf", "tests/fixtures/rtf/semantic.rtf"])
def test_public_rtf_entry_uses_native_and_matches_frozen_lexer(monkeypatch: pytest.MonkeyPatch, relative: str) -> None:
    """真实和完整语义 RTF 通过公开入口命中原生扫描，并与冻结组扫描的完整结果一致。"""
    source = (ROOT / relative).read_bytes()
    native = get_native()
    before = native.rtf_group_stats()
    actual = parse(source, file_suffix="rtf", keep_model_json=True)
    after = native.rtf_group_stats()
    assert after[0] > before[0]
    assert after[1] > before[1]
    assert after[3] == before[3]

    def frozen_groups(data: bytes, *, direct_only: bool = False):
        """仅替换组发现为冻结词法实现，正文、投影和公开输出仍走真实调用链。"""
        spans, error = _expected(data, direct_only, lexer.MAX_RECORDS, lexer.MAX_RTF_GROUP_DEPTH)
        assert error is None
        return [parser._GroupSpan(data, start, end) for start, end in spans]

    monkeypatch.setattr(parser, "_group_spans", frozen_groups)
    expected = parse(source, file_suffix="rtf", keep_model_json=True)
    assert _full_output(actual) == _full_output(expected)


def test_frozen_lexer_still_matches_unmodified_public_generator() -> None:
    """组扫描迁移不修改公开 lexer；冻结文件身份和独立预算保持可追溯。"""
    source = Path(lexer.__file__).read_text(encoding="utf-8")
    original_prefix = source[: source.index("_ORIGINAL_LEXER =")]
    original_prefix = original_prefix.replace("import sys\n", "").replace("from ....._compute_backend import get_native\n", "")
    original_prefix = original_prefix.rstrip() + "\n\n\n" + source[source.index("__all__ =") :]
    assert hashlib.sha256(original_prefix.encode()).hexdigest() == oracle.ORIGINAL_LEXER_SHA256
