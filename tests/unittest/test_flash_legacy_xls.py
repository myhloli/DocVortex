from __future__ import annotations

import struct
from io import BytesIO

import pytest
from _legacy_ppt_test_utils import _build_cfb
from _legacy_xls_test_utils import (
    SheetFixture,
    biff_record,
    build_biff5_xls,
    build_xls,
    continued_rich_sst,
    font_record,
    formula_number_cell,
    formula_string_cell,
    label_cell,
    labelsst_cell,
    merged_cells,
    rich_sst,
    url_hyperlink,
)
from _span_test_utils import inline
from bs4 import BeautifulSoup

from docvortex.analyzers.native import XlsModel
from docvortex.analyzers.native._shared.hyperlink import OFFICE_EXTERNAL_HYPERLINK_SCHEMES, sanitize_hyperlink_target
from docvortex.analyzers.native.office.errors import (
    LegacyOfficeEncryptedError,
    LegacyOfficeMissingPartError,
    LegacyOfficeResourceLimitError,
)
from docvortex.analyzers.native.office.limits import MAX_RECORDS
from docvortex.analyzers.native.office.xls import xls_converter as xls_converter_module
from docvortex.analyzers.native.office.xls.number_format import format_number, format_text
from docvortex.analyzers.native.office.xls.records import RecordBudget
from docvortex.schema import BlockType


def test_xls_model_preserves_visible_empty_pages_and_skips_hidden_sheet() -> None:
    """Verify that visible empty tables reserve page bits and hide sheet without output."""

    file_bytes = build_xls(
        [
            SheetFixture("Visible", label_cell(0, 0, "visible")),
            SheetFixture("Empty"),
            SheetFixture("Secret", label_cell(0, 0, "secret"), visible=False),
        ]
    )
    stream = BytesIO(file_bytes)

    pages = XlsModel().predict(stream)

    assert not stream.closed
    assert pages == [[{"type": BlockType.TEXT, "content": inline("visible")}], []]


def test_xls_formula_cache_merge_and_hyperlink_flow_into_table() -> None:
    """Verify that cached formulas, merge structures, and secure links go into the same HTML table."""

    records = (
        label_cell(0, 0, "header")
        + label_cell(0, 1, "link")
        + formula_number_cell(1, 0, 21.0)
        + formula_string_cell(1, 1, "#NAME?")
        + url_hyperlink(0, 1, "link", "https://example.test/path")
        + merged_cells((2, 0, 2, 1))
        + label_cell(2, 0, "merged")
    )

    pages = XlsModel().predict(BytesIO(build_xls([SheetFixture("Data", records)])))
    table = next(block for block in pages[0] if block["type"] == BlockType.TABLE)
    soup = BeautifulSoup(table["content"], "html.parser")

    assert soup.get_text(" ", strip=True).split() == [
        "header",
        "link",
        "21",
        "#NAME?",
        "merged",
    ]
    assert soup.find("a")["href"] == "https://example.test/path"
    assert soup.find(string="merged").find_parent(["th", "td"])["colspan"] == "2"


def test_xls_rich_sst_uses_utf16_ranges_across_non_bmp_text() -> None:
    """Verify that UTF-16 rich run boundaries for non-BMP characters are not offset."""

    globals_records = font_record(bold=True) + font_record(italic=True) + rich_sst([("A😀B", [(0, 1), (3, 2)])])
    pages = XlsModel().predict(
        BytesIO(
            build_xls(
                [SheetFixture("Rich", labelsst_cell(0, 0, 0))],
                globals_records=globals_records,
            )
        )
    )

    table = next(block for block in pages[0] if block["type"] == BlockType.TABLE)
    assert "<strong>A😀</strong>" in table["content"]
    assert "<em>B</em>" in table["content"]


def test_xls_sst_character_data_can_cross_continue_records() -> None:
    """Verify SST Reread the compression flag after cutting mid-character CONTINUE and continue rich runs."""

    globals_records = font_record(bold=True) + font_record(italic=True) + continued_rich_sst("A😀BC", [(0, 1), (3, 2)])
    pages = XlsModel().predict(
        BytesIO(
            build_xls(
                [SheetFixture("Rich", labelsst_cell(0, 0, 0))],
                globals_records=globals_records,
            )
        )
    )

    table = next(block for block in pages[0] if block["type"] == BlockType.TABLE)
    assert "<strong>A😀</strong>" in table["content"]
    assert "<em>BC</em>" in table["content"]


@pytest.mark.parametrize(
    ("format_code", "value", "expected"),
    [
        ("0.0%", 0.075, "7.5%"),
        ("#,##0.00", 1234.5, "1,234.50"),
        ('"$"#,##0.00', 1234.5, "$1,234.50"),
        ("0.00;(0.00)", -3.5, "(3.50)"),
        ("0.00E+00", 12345.0, "1.23E+04"),
        ("# ?/?", 5.25, "5 1/4"),
        ("# ?/8", 5.25, "5 2/8"),
        ("0.0,,", 12_345_678.0, "12.3"),
        ("0.00_);(0.00)", 3.5, "3.50 "),
        ('0.0\\ "m/s"', 3.51, "3.5 m/s"),
        ('"~"General" kg"', 1234.5, "~1234.5 kg"),
        ("yyyy-mm-dd", 46096.0, "2026-03-15"),
        ("[h]:mm:ss", 1.5, "36:00:00"),
    ],
)
def test_xls_number_formats_match_expected_visible_semantics(
    format_code: str,
    value: float,
    expected: str,
) -> None:
    """Verify that the key value format conforms to the expected stable display semantics."""

    assert format_number(value, format_code, date1904=False) == expected


def test_xls_text_format_and_unsafe_hyperlink_fallback() -> None:
    """Verify that the text section is in effect and the dangerous link is downgraded to normal text."""

    assert format_text("hi", '0;0;0;"* "@" *"') == "* hi *"
    records = label_cell(0, 0, "unsafe") + url_hyperlink(
        0,
        0,
        "unsafe",
        "javascript:alert(1)",
    )
    pages = XlsModel().predict(BytesIO(build_xls([SheetFixture("Data", records)])))
    assert pages == [[{"type": BlockType.TEXT, "content": inline("unsafe")}]]
    policy = {
        "allowed_schemes": OFFICE_EXTERNAL_HYPERLINK_SCHEMES,
        "allow_relative": True,
        "allow_fragment": True,
    }
    assert sanitize_hyperlink_target("mailto:user@example.test", **policy) == "mailto:user@example.test"
    assert sanitize_hyperlink_target("#Sheet2!A1", **policy) == "#Sheet2!A1"
    assert sanitize_hyperlink_target("../relative/path", **policy) == "../relative/path"
    assert sanitize_hyperlink_target("file:///tmp/local.xls", **policy) is None
    assert sanitize_hyperlink_target("custom:payload", **policy) is None


def test_xls_filepass_and_broken_boundsheet_behaviors() -> None:
    """Verification encryption hard failed and corrupted sheet offset recoverable by worksheet BOF."""

    with pytest.raises(LegacyOfficeEncryptedError, match="password-protected"):
        XlsModel().predict(BytesIO(build_xls([SheetFixture("Data")], encrypted=True)))

    pages = XlsModel().predict(
        BytesIO(
            build_xls(
                [SheetFixture("Recovered", label_cell(0, 0, "ok"))],
                corrupt_first_offset=True,
            )
        )
    )
    assert pages == [[{"type": BlockType.TEXT, "content": inline("ok")}]]


def test_xls_encrypted_ooxml_marker_and_missing_workbook_are_stable_errors() -> None:
    """Verify that the OLE encrypted package will not be misjudged as BIFF, and a stable error will be returned if Workbook/Book is missing."""

    encrypted = _build_cfb(
        [
            ("EncryptionInfo", b"marker"),
            ("EncryptedPackage", b"payload"),
        ]
    )
    with pytest.raises(LegacyOfficeEncryptedError, match="password-protected"):
        XlsModel().predict(BytesIO(encrypted))

    with pytest.raises(LegacyOfficeMissingPartError, match="Book"):
        XlsModel().predict(BytesIO(_build_cfb([("Other", b"payload")])))


def test_xls_biff5_codepage_and_hidden_rows_columns_are_preserved() -> None:
    """Verify BIFF5 codepage downgrade and user-selected hidden column retention policy."""

    assert XlsModel().predict(BytesIO(build_biff5_xls("légacy"))) == [[{"type": BlockType.TEXT, "content": inline("légacy")}]]

    row = bytearray(16)
    struct.pack_into("<H", row, 0, 0)
    row[12] = 0x20
    colinfo = bytearray(12)
    struct.pack_into("<HH", colinfo, 0, 0, 0)
    colinfo[8] = 0x01
    records = biff_record(0x0208, bytes(row)) + biff_record(0x007D, bytes(colinfo)) + label_cell(0, 0, "still visible")

    assert XlsModel().predict(BytesIO(build_xls([SheetFixture("Data", records)]))) == [
        [{"type": BlockType.TEXT, "content": inline("still visible")}]
    ]


def test_xls_record_and_grid_limits_are_hard_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither validation record access nor workbook grid budgeting can be silently bypassed."""

    budget = RecordBudget(count=MAX_RECORDS)
    with pytest.raises(LegacyOfficeResourceLimitError, match="max_records"):
        budget.charge()

    monkeypatch.setattr(xls_converter_module, "MAX_GRID_SLOTS", 0)
    with pytest.raises(LegacyOfficeResourceLimitError, match="max_grid_slots"):
        XlsModel().predict(BytesIO(build_xls([SheetFixture("Data", label_cell(0, 0, "value"))])))


def test_xls_rejects_oversized_merge_before_openpyxl_materialization(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that a single out-of-limit BIFF merge does not materialize into a openpyxl cell."""
    monkeypatch.setattr(xls_converter_module, "MAX_GRID_SLOTS", 4)

    def unexpected_merge(*_args: object, **_kwargs: object) -> None:
        """merge out of limit openpyxl must not be called."""
        pytest.fail("oversized BIFF merge reached openpyxl materialization")

    monkeypatch.setattr(xls_converter_module.Worksheet, "merge_cells", unexpected_merge)
    source = build_xls([SheetFixture("Data", merged_cells((0, 0, 2, 1)))])

    with pytest.raises(LegacyOfficeResourceLimitError, match="max_grid_slots"):
        XlsModel().predict(BytesIO(source))


def test_xls_charges_cumulative_merge_budget_before_each_materialization(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifying the cumulative area of multiple legal merges triggers the budget before the next materialization."""
    monkeypatch.setattr(xls_converter_module, "MAX_GRID_SLOTS", 4)
    original_merge = xls_converter_module.Worksheet.merge_cells
    materialized: list[tuple[object, ...]] = []

    def tracking_merge(self: object, *args: object, **kwargs: object) -> None:
        """Record merge that actually goes into openpyxl within budget."""
        materialized.append(args or tuple(kwargs.values()))
        original_merge(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(xls_converter_module.Worksheet, "merge_cells", tracking_merge)
    source = build_xls(
        [
            SheetFixture(
                "Data",
                merged_cells(
                    (0, 0, 0, 1),
                    (1, 0, 1, 1),
                    (2, 0, 2, 1),
                ),
            )
        ]
    )

    with pytest.raises(LegacyOfficeResourceLimitError, match="max_grid_slots"):
        XlsModel().predict(BytesIO(source))
    assert len(materialized) == 2
