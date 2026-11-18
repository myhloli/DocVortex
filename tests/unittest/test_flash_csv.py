from __future__ import annotations

import codecs
from io import BytesIO
from pathlib import Path

import pytest
from _native_test_utils import analyze_native_test_document
from bs4 import BeautifulSoup

from docvortex.analyzers.native import CsvModel
from docvortex.analyzers.native import csv as csv_module
from docvortex.document.detection import guess_suffix_by_bytes, guess_suffix_by_path
from docvortex.render.html import render_html
from docvortex.render.markdown import render_markdown
from docvortex.schema import BlockType


def _raw_table_html(payload: bytes) -> str:
    """解析 CSV 并返回 model-list 中唯一表格的 HTML。"""
    pages = CsvModel().predict(BytesIO(payload))
    assert len(pages) == 1
    assert len(pages[0]) == 1
    assert pages[0][0]["type"] == BlockType.TABLE
    return pages[0][0]["content"]


def _html_rows(payload: bytes) -> list[list[str]]:
    """把 CSV 投影 HTML 还原为逐行单元格纯文本，方便断言语义。"""
    soup = BeautifulSoup(_raw_table_html(payload), "html.parser")
    return [[cell.get_text("\n") for cell in row.find_all(["th", "td"])] for row in soup.find_all("tr")]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (codecs.BOM_UTF8 + "姓名,年龄\n张三,30\n".encode(), [["姓名", "年龄"], ["张三", "30"]]),
        (codecs.BOM_UTF16_LE + "姓名;年龄\n张三;30\n".encode("utf-16le"), [["姓名", "年龄"], ["张三", "30"]]),
        ("姓名|年龄\n张三|30\n".encode("gb18030"), [["姓名", "年龄"], ["张三", "30"]]),
        ("name\tcity\nAndré\tZürich\n".encode("cp1252"), [["name", "city"], ["André", "Zürich"]]),
    ],
)
def test_csv_decodes_common_encodings_and_delimiters(payload: bytes, expected: list[list[str]]) -> None:
    """验证常见中西文编码和四种分隔符都进入同一表格语义。"""
    assert _html_rows(payload) == expected


def test_csv_sep_directive_overrides_delimiter_sniffing() -> None:
    """验证 Excel sep 指令只控制分隔符，不作为数据行输出。"""
    payload = 'sep=;\r\nname;note\r\nAlice;"1,2"\r\n'.encode()

    assert _html_rows(payload) == [["name", "note"], ["Alice", "1,2"]]


def test_csv_preserves_multiline_quotes_padding_ragged_rows_and_safe_text() -> None:
    """验证多行字段、引号、前导零、首尾空格、短行补齐和 HTML 转义。"""
    payload = (
        'name,note,code,markup\nAlice,"line 1\nline 2",001,"<script>alert(1)</script>"\nBob,"  say ""hi""  ",02\n'
    ).encode()

    table_html = _raw_table_html(payload)
    assert "line 1<br>line 2" in table_html
    assert "<script>" not in table_html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in table_html
    assert _html_rows(payload) == [
        ["name", "note", "code", "markup"],
        ["Alice", "line 1\nline 2", "001", "<script>alert(1)</script>"],
        ["Bob", '  say "hi"  ', "02", ""],
    ]

    middle, _ = analyze_native_test_document(payload, file_suffix="csv")
    markdown = render_markdown(middle)
    standalone_html = render_html(middle)
    assert "<script>alert(1)</script>" not in markdown
    assert "&lt;script>alert(1)&lt;/script>" in markdown
    assert "<script>alert(1)</script>" not in standalone_html


@pytest.mark.parametrize(
    ("payload", "expected_header"),
    [
        (b"name,age\nAlice,30\nBob,40\n", True),
        (b"name,city\nAlice,London\nBob,Paris\n", True),
        (b"1,10\n2,20\n3,30\n", False),
        (b"only,one,row\n", False),
    ],
)
def test_csv_header_inference_is_deterministic(payload: bytes, expected_header: bool) -> None:
    """验证有类型证据、纯文本标签、纯数据和单行文件的表头边界。"""
    soup = BeautifulSoup(_raw_table_html(payload), "html.parser")
    assert bool(soup.find("th")) is expected_header


def test_csv_empty_single_column_and_blank_records_keep_one_logical_page() -> None:
    """验证空 CSV、单列 CSV 和空记录都保留确定的一页语义。"""
    assert CsvModel().predict(BytesIO(b"")) == [[]]
    assert _html_rows(b"value\none\n\ntwo\n") == [["value"], ["one"], [""], ["two"]]


@pytest.mark.parametrize(
    "payload",
    [
        b'a,b\n1,"unterminated\n',
        b"a,b\n1,\x81\n",
    ],
)
def test_csv_rejects_malformed_syntax_and_unsupported_encoding(payload: bytes) -> None:
    """验证损坏引号或无法严格解码的字节会让整份 CSV 失败。"""
    with pytest.raises(ValueError):
        CsvModel().predict(BytesIO(payload))


@pytest.mark.parametrize(
    ("constant", "limit", "payload", "message"),
    [
        ("MAX_CSV_BYTES", 3, b"a,b\n", "max_bytes"),
        ("MAX_CSV_ROWS", 1, b"a\nb\n", "max_rows"),
        ("MAX_CSV_COLUMNS", 1, b"a,b\n", "max_columns"),
        ("MAX_CSV_GRID_SLOTS", 3, b"a,b\n1,2\n", "max_grid_slots"),
    ],
)
def test_csv_enforces_resource_limits(
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    limit: int,
    payload: bytes,
    message: str,
) -> None:
    """验证输入、行、列和规则化网格限制都采用显式失败。"""
    monkeypatch.setattr(csv_module, constant, limit)

    with pytest.raises(ValueError, match=message):
        CsvModel().predict(BytesIO(payload))


def test_csv_grid_limit_short_circuits_before_trailing_malformed_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证网格超限后立即失败，不再继续读取尾部损坏记录。"""
    monkeypatch.setattr(csv_module, "MAX_CSV_GRID_SLOTS", 3)

    with pytest.raises(ValueError, match="max_grid_slots"):
        CsvModel().predict(BytesIO(b'a,b\n1,2\n"unterminated'))


def test_csv_rendered_budget_fails_before_materializing_escaped_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 HTML 展开超限时不会先创建放大的字段字符串。"""
    monkeypatch.setattr(csv_module, "MAX_CSV_RENDERED_BYTES", 64)

    def unexpected_escape(_value: str) -> str:
        """输出预算应在进入 html.escape 前拒绝字段。"""
        pytest.fail("oversized CSV field reached HTML escaping")

    monkeypatch.setattr(csv_module, "_render_field_html", unexpected_escape)

    with pytest.raises(ValueError, match="max_rendered_bytes"):
        csv_module._rows_to_html([["&" * 20]], has_header=False)


def test_csv_rendered_size_estimator_matches_html_escape_semantics() -> None:
    """验证特殊字符、换行、控制符和非 ASCII 文本的 UTF-8 预算精确。"""
    value = "&<>\"'\r\n\x01中"
    rendered = csv_module._render_field_html(value)

    assert csv_module._rendered_field_utf8_bytes(value, 1_000) == len(rendered.encode())


def test_csv_default_grid_budget_rejects_wide_dom_before_rendering() -> None:
    """验证默认预算在宽空表生成数十万 HTML 节点前拒绝输入。"""
    assert csv_module.MAX_CSV_GRID_SLOTS == 250_000
    wide_empty_row = b"," * (csv_module.MAX_CSV_COLUMNS - 1) + b"\n"
    payload = wide_empty_row * 16

    with pytest.raises(ValueError, match="max_grid_slots"):
        CsvModel().predict(BytesIO(payload))


def test_delimited_extension_resolves_to_independent_suffix() -> None:
    """以 .csv/.tsv 路径兜底时按扩展名返回独立后缀，不再把 tsv 折叠为 csv。"""
    payload = b"name,city\nAlice,London\n"

    assert guess_suffix_by_bytes(payload, "demo.csv") == "csv"
    assert guess_suffix_by_bytes(payload, "demo.tsv") == "tsv"


def test_tsv_signatureless_bytes_without_path_require_explicit_suffix() -> None:
    """无路径字节流不自动进入 tsv 解析，必须显式 file_suffix（与 csv 对称）。"""
    payload = b"name\tcity\nAlice\tLondon\nBob\tParis\n" * 5

    assert guess_suffix_by_bytes(payload) == "txt"


def test_strong_content_signature_overrides_tsv_extension_fallback() -> None:
    """RTF 强内容签名必须覆盖 .tsv 的无签名扩展名兜底。"""
    payload = b"\xef\xbb\xbf \r\n{\\RTF1\\ANSI body}"

    assert guess_suffix_by_bytes(payload, "disguised.tsv") == "rtf"


def test_tsv_path_detection_returns_independent_suffix(tmp_path: Path) -> None:
    """真实 .tsv 路径按独立后缀识别。"""
    tsv_path = tmp_path / "demo.tsv"
    tsv_path.write_bytes("姓名\t年龄\n张三\t30\n李四\t40\n".encode())

    assert guess_suffix_by_path(tsv_path) == "tsv"


def test_tsv_uses_shared_csv_engine_and_records_independent_file_suffix() -> None:
    """tsv 复用 CSV 引擎与自动分隔符嗅探，并如实记录独立 file_suffix。"""
    payload = "姓名\t年龄\n张三\t30\n李四\t40\n".encode()
    middle, _ = analyze_native_test_document(payload, file_suffix="tsv")

    assert middle.metadata.file_suffix == "tsv"
    assert len(middle.pages) == 1
    markdown = render_markdown(middle)
    assert "张三" in markdown
    assert "30" in markdown


def test_csv_ascii_and_escaped_fields_share_exact_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """普通 ASCII 与需转义字段混排时，最后一个输出字节仍计入同一预算。"""
    rows = [[" /+%,-.", "A\t&B"], ["中文", "x\r\ny"]]
    expected = csv_module._rows_to_html(rows, has_header=True)
    size = len(expected.encode("utf-8"))
    monkeypatch.setattr(csv_module, "MAX_CSV_RENDERED_BYTES", size)
    assert csv_module._rows_to_html(rows, has_header=True) == expected
    monkeypatch.setattr(csv_module, "MAX_CSV_RENDERED_BYTES", size - 1)
    with pytest.raises(ValueError, match="max_rendered_bytes"):
        csv_module._rows_to_html(rows, has_header=True)
