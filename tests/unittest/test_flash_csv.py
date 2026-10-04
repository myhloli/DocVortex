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
    """Parses CSV and returns HTML for the only table in model-list."""
    pages = CsvModel().predict(BytesIO(payload))
    assert len(pages) == 1
    assert len(pages[0]) == 1
    assert pages[0][0]["type"] == BlockType.TABLE
    return pages[0][0]["content"]


def _html_rows(payload: bytes) -> list[list[str]]:
    """Restore CSV projection HTML to line-by-line cell plain text to facilitate assertion semantics."""
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
    """Verify that common Chinese and Western encodings and four delimiters enter the same table semantics."""
    assert _html_rows(payload) == expected


def test_csv_sep_directive_overrides_delimiter_sniffing() -> None:
    """Verification Excel The sep command only controls the delimiter and is not output as a data line."""
    payload = 'sep=;\r\nname;note\r\nAlice;"1,2"\r\n'.encode()

    assert _html_rows(payload) == [["name", "note"], ["Alice", "1,2"]]


def test_csv_preserves_multiline_quotes_padding_ragged_rows_and_safe_text() -> None:
    """Validates multiline fields, quotes, leading zeros, leading and trailing spaces, short line padding, and HTML escaping."""
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
    """Validates header boundaries with type evidence, plain text labels, plain data, and single-line files."""
    soup = BeautifulSoup(_raw_table_html(payload), "html.parser")
    assert bool(soup.find("th")) is expected_header


def test_csv_empty_single_column_and_blank_records_keep_one_logical_page() -> None:
    """Verify that empty CSV, single-column CSV, and empty records all preserve certain one-page semantics."""
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
    """Validating broken quotes or bytes that cannot be strictly decoded will fail the entire CSV."""
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
    """Validation of input, row, column, and regularized grid constraints all fail explicitly."""
    monkeypatch.setattr(csv_module, constant, limit)

    with pytest.raises(ValueError, match=message):
        CsvModel().predict(BytesIO(payload))


def test_csv_grid_limit_short_circuits_before_trailing_malformed_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verification fails immediately after the grid exceeds the limit, and no longer continues to read the tail damaged records."""
    monkeypatch.setattr(csv_module, "MAX_CSV_GRID_SLOTS", 3)

    with pytest.raises(ValueError, match="max_grid_slots"):
        CsvModel().predict(BytesIO(b'a,b\n1,2\n"unterminated'))


def test_csv_rendered_budget_fails_before_materializing_escaped_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verification HTML Expansion overrun does not create the expanded field string first."""
    monkeypatch.setattr(csv_module, "MAX_CSV_RENDERED_BYTES", 64)

    def unexpected_escape(_value: str) -> str:
        """The output budget should reject fields before entering html.escape."""
        pytest.fail("oversized CSV field reached HTML escaping")

    monkeypatch.setattr(csv_module, "_render_field_html", unexpected_escape)

    with pytest.raises(ValueError, match="max_rendered_bytes"):
        csv_module._rows_to_html([["&" * 20]], has_header=False)


def test_csv_rendered_size_estimator_matches_html_escape_semantics() -> None:
    """Verify that UTF-8 budgets for special characters, line breaks, control characters, and non-ASCII text are accurate."""
    value = "&<>\"'\r\n\x01中"
    rendered = csv_module._render_field_html(value)

    assert csv_module._rendered_field_utf8_bytes(value, 1_000) == len(rendered.encode())


def test_csv_default_grid_budget_rejects_wide_dom_before_rendering() -> None:
    """Verify that the default budget rejects input before generating hundreds of thousands of HTML nodes in the wide-empty table."""
    assert csv_module.MAX_CSV_GRID_SLOTS == 250_000
    wide_empty_row = b"," * (csv_module.MAX_CSV_COLUMNS - 1) + b"\n"
    payload = wide_empty_row * 16

    with pytest.raises(ValueError, match="max_grid_slots"):
        CsvModel().predict(BytesIO(payload))


def test_delimited_extension_resolves_to_independent_suffix() -> None:
    """When using the .csv/.tsv path, the independent suffix will be returned according to the extension, and tsv will no longer be folded into csv."""
    payload = b"name,city\nAlice,London\n"

    assert guess_suffix_by_bytes(payload, "demo.csv") == "csv"
    assert guess_suffix_by_bytes(payload, "demo.tsv") == "tsv"


def test_tsv_signatureless_bytes_without_path_require_explicit_suffix() -> None:
    """Pathless byte streams do not automatically enter tsv parsing and must explicitly file_suffix (symmetrical to csv)."""
    payload = b"name\tcity\nAlice\tLondon\nBob\tParis\n" * 5

    assert guess_suffix_by_bytes(payload) == "txt"


def test_strong_content_signature_overrides_tsv_extension_fallback() -> None:
    """RTF Strong content signature must override the unsigned extension of .tsv."""
    payload = b"\xef\xbb\xbf \r\n{\\RTF1\\ANSI body}"

    assert guess_suffix_by_bytes(payload, "disguised.tsv") == "rtf"


def test_tsv_path_detection_returns_independent_suffix(tmp_path: Path) -> None:
    """Real .tsv paths are identified by a separate suffix."""
    tsv_path = tmp_path / "demo.tsv"
    tsv_path.write_bytes("姓名\t年龄\n张三\t30\n李四\t40\n".encode())

    assert guess_suffix_by_path(tsv_path) == "tsv"


def test_tsv_uses_shared_csv_engine_and_records_independent_file_suffix() -> None:
    """tsv reuses the CSV engine with automatic delimiter sniffing and faithfully records the standalone file_suffix."""
    payload = "姓名\t年龄\n张三\t30\n李四\t40\n".encode()
    middle, _ = analyze_native_test_document(payload, file_suffix="tsv")

    assert middle.metadata.file_suffix == "tsv"
    assert len(middle.pages) == 1
    markdown = render_markdown(middle)
    assert "张三" in markdown
    assert "30" in markdown
