from __future__ import annotations

import subprocess
import sys
import tracemalloc
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import pytest
from _native_test_utils import analyze_native_test_document
from _span_test_utils import inline, inline_text

from docvortex.analyzers.native import RtfModel
from docvortex.analyzers.native.office.errors import LegacyOfficeMalformedError, LegacyOfficeResourceLimitError
from docvortex.analyzers.native.office.rtf import lexer as lexer_module
from docvortex.analyzers.native.office.rtf import parser as parser_module
from docvortex.analyzers.native.office.rtf.converter import extract_rtf_metadata
from docvortex.analyzers.native.office.rtf.lexer import RtfBinary, RtfLexer
from docvortex.render import render_docx, render_html, render_markdown
from docvortex.schema import BlockType

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


_SEMANTIC_RTF = _PROJECT_ROOT / "tests" / "fixtures" / "rtf" / "semantic.rtf"


_PNG_HEX = (
    b"89504e470d0a1a0a0000000d494844520000000200000002080600000072b60d24"
    b"0000001549444154789c6394acb8f39f81818181094480300024350270cd4262d30000000049454e44ae426082"
)


def _complex_rtf() -> bytes:
    """Reads deterministic RTF covering titles, lists, tables, formulas, notes, links and images."""
    return _SEMANTIC_RTF.read_bytes()


def test_rtf_lexer_bin_payload_is_position_explicit() -> None:
    """Verify that the curly braces and backslashes in bin do not change the lexer group structure."""
    tokens = list(RtfLexer(rb"{\rtf1 before\bin5 }}{\x after}"))
    binaries = [token for token in tokens if isinstance(token, RtfBinary)]

    assert [token.data for token in binaries] == [b"}}{\\x"]


def test_rtf_lexer_rejects_truncated_bin_payload() -> None:
    """Validation truncation bin failed immediately before the cursor lost trusted boundaries."""
    with pytest.raises(LegacyOfficeMalformedError, match="truncated"):
        list(RtfLexer(rb"{\rtf1\bin9 ab}"))


@pytest.mark.parametrize(
    "control",
    [
        b"\\foo99999999999",
        b"\\foo2147483648",
        b"\\foo-2147483649",
        b"\\bin99999999999 payload",
    ],
)
def test_rtf_lexer_rejects_control_parameters_before_unbounded_int(control: bytes) -> None:
    """Validation overlong or out-of-bounds control parameter failed before integer conversion and bin positioning."""
    with pytest.raises(LegacyOfficeResourceLimitError, match="control parameter"):
        list(RtfLexer(b"{\\rtf1" + control + b"}"))


def test_rtf_lexer_accepts_signed_32_bit_control_parameter_boundaries() -> None:
    """Verify that the ten-bit lexical limit still allows signed 32-bit parameter boundaries."""
    tokens = list(RtfLexer(rb"{\rtf1\foo2147483647\bar-2147483648}"))
    controls = [token for token in tokens if isinstance(token, lexer_module.RtfControlWord)]

    assert [(token.name, token.param) for token in controls[-2:]] == [
        ("foo", 2_147_483_647),
        ("bar", -2_147_483_648),
    ]


def test_rtf_font_codepages_and_unicode_surrogates_are_exact() -> None:
    """Verify per-font, CP1251/CP932, multibyte hex, and UTF-16 agent pair recovery."""
    russian = b"".join(f"\\'{value:02x}".encode("ascii") for value in "Привет".encode("cp1251"))
    japanese = b"".join(f"\\'{value:02x}".encode("ascii") for value in "こんにちは".encode("cp932"))
    source = b"".join(
        [
            rb"{\rtf1\ansi{\fonttbl{\f0\fcharset204 Arial;}{\f1\fcharset128 MS Gothic;}}",
            rb"\pard\f0 ",
            russian,
            rb" \f1 ",
            japanese,
            rb" \u-10179?\u-8704?\par}",
        ]
    )

    pages = RtfModel().predict(BytesIO(source))

    assert pages == [[{"type": BlockType.TEXT, "content": inline("Привет こんにちは 😀")}]]


def test_rtf_metadata_decodes_consecutive_multibyte_hex_as_one_run() -> None:
    """Verify that metadata contiguous GB18030 hex byte in the definition group decodes in full code page sequence."""
    encoded_title = b"".join(f"\\'{value:02x}".encode("ascii") for value in "中文".encode("gb18030"))
    source = b"".join(
        [
            rb"{\rtf1\ansi\ansicpg936{\info{\title ",
            encoded_title,
            rb"}}Body\par}",
        ]
    )

    metadata = extract_rtf_metadata(BytesIO(source))
    pages = RtfModel().predict(BytesIO(source))

    assert metadata["title"] == "中文"
    assert pages == [[{"type": BlockType.TEXT, "content": inline("Body")}]]


def test_rtf_unicode_controls_buffer_adjacent_text_without_repeated_run_copy() -> None:
    """Validate character by character Unicode control Materialize text only at boundaries run."""
    count = 5_000
    source = b"{\\rtf1\\ansi\\uc1 " + b"\\u20013?" * count + b"\\par}"

    with patch.object(parser_module, "replace", wraps=parser_module.replace) as replace_mock:
        pages = RtfModel().predict(BytesIO(source))

    text_replacements = [
        call
        for call in replace_mock.call_args_list
        if isinstance(call.args[0], parser_module.RtfTextRun) and "text" in call.kwargs
    ]
    assert pages == [[{"type": BlockType.TEXT, "content": inline("中" * count)}]]
    assert text_replacements == []


def test_rtf_upr_prefers_unicode_branch_without_leaking_ansi_fallback() -> None:
    """Verify that Unicode-aware parser only outputs the ud branch in upr."""
    source = rb"{\rtf1\ansi Before {\upr{?}{\*\ud{\u20013?}}} After\par}"

    pages = RtfModel().predict(BytesIO(source))

    assert pages == [[{"type": BlockType.TEXT, "content": inline("Before 中 After")}]]


def test_rtf_inherited_style_honors_explicit_formatting_resets() -> None:
    """Verify that derived stylesheet and b0/i0/ul0/strike0 override the base style on value."""
    source = (
        rb"{\rtf1\ansi{\stylesheet{\s0\b\i\ul\strike Base;}{\s1\sbasedon0\b0\i0\ul0\strike0 Derived;}}\pard\s1 Reset text\par}"
    )

    pages = RtfModel().predict(BytesIO(source))

    assert pages == [[{"type": BlockType.TEXT, "content": inline("Reset text")}]]


def test_rtf_large_roman_list_start_falls_back_to_decimal() -> None:
    """Verify that Roman starting values outside the specification range are directly rolled back to decimal without linear expansion."""
    source = b"".join(
        [
            rb"{\rtf1\ansi{\*\listtable{\list{\listlevel\levelnfc1\levelstartat2147483647}\listid7}}",
            rb"{\*\listoverridetable{\listoverride\listid7\listoverridecount0\ls2}}",
            rb"\pard\ls2\ilvl0 item\par}",
        ]
    )

    pages = RtfModel().predict(BytesIO(source))

    assert pages[0][0]["type"] == BlockType.LIST
    assert pages[0][0]["content"][0]["list_label"] == "2147483647."


def test_rtf_model_recovers_unicode_styles_and_structures() -> None:
    """Verify that the complete typed RTF link generates a stable single page raw blocks."""
    stream = BytesIO(_complex_rtf())

    pages = RtfModel().predict(stream)

    assert not stream.closed
    assert len(pages) == 1
    blocks = pages[0]
    assert [block["type"] for block in blocks].count(BlockType.TABLE) == 1
    assert [block["type"] for block in blocks].count(BlockType.IMAGE) == 1
    assert [block["type"] for block in blocks].count(BlockType.CODE) == 1
    title = blocks[0]
    assert title["type"] == BlockType.PARAGRAPH_TITLE
    assert title["level"] == 2
    assert title["anchor"] == "heading"
    assert any(set(span.get("styles", [])) == {"bold", "italic"} for span in title["content"])
    body = next(
        block for block in blocks if block.get("type") == BlockType.TEXT and "Unicode" in inline_text(block.get("content"))
    )
    assert "中文" in inline_text(body["content"])
    assert any("superscript" in span.get("styles", []) for span in body["content"])
    link = next(
        block for block in blocks if block.get("type") == BlockType.TEXT and "Jump heading" in inline_text(block.get("content"))
    )
    assert any(span.get("type") == "hyperlink" and span.get("url") == "#heading" for span in link["content"])
    assert "javascript:" not in str(link["content"])
    assert blocks[-3:] == [
        {"type": BlockType.HEADER, "content": inline("Header text")},
        {"type": BlockType.FOOTER, "content": inline("Footer text")},
        {"type": BlockType.PAGE_FOOTNOTE, "content": inline("[1] Foot body.")},
    ]
    code = next(block for block in blocks if block.get("type") == BlockType.CODE)
    assert code["content"] == "first()\nsecond()"
    table = next(block for block in blocks if block.get("type") == BlockType.TABLE)
    assert 'colspan="2"' in table["content"]
    assert "<th" in table["content"]


def test_rtf_page_controls_remain_inside_one_semantic_page() -> None:
    """Verify that page/column only retains line breaks, and sect only ends paragraphs."""
    pages = RtfModel().predict(BytesIO(rb"{\rtf1\ansi A\page B\column C\sect D\par}"))

    assert len(pages) == 1
    assert pages[0] == [
        {"type": BlockType.TEXT, "content": inline("A\nB\nC")},
        {"type": BlockType.TEXT, "content": inline("D")},
    ]


def test_rtf_nested_and_merged_tables_preserve_content() -> None:
    """Verify that nested table, cellx projection and portrait continuation do not lose visible text."""
    source = rb"""{\rtf1\ansi
\trowd\clvmgf\cellx1000\cellx2000 A\cell B\cell\row
\trowd\clvmrg\cellx1000\cellx2000 \cell C\cell\row
\trowd\cellx1000\cellx2000
\pard\intbl\itap2 N1\nestcell N2\nestcell{\*\nesttableprops\trowd\cellx500\cellx1000\nestrow}
\pard\intbl outer\cell\row}"""

    pages = RtfModel().predict(BytesIO(source))

    tables = [block for block in pages[0] if block["type"] == BlockType.TABLE]
    assert len(tables) == 1
    html = tables[0]["content"]
    assert 'rowspan="2"' in html
    assert html.count("<table>") == 2
    assert all(text in html for text in ("A", "B", "C", "N1", "N2", "outer"))


def test_rtf_math_picture_is_only_used_when_formula_conversion_fails() -> None:
    """Verify Office Math Deduplicate preview on success, keep first safe fallback image on failure."""
    valid = b"".join(
        [
            rb"{\rtf1{\mmath{\*\moMath{\mr x}}{\mmathPict{\pict\pngblip ",
            _PNG_HEX,
            rb"}}}}",
        ]
    )
    invalid = b"".join(
        [
            rb"{\rtf1{\mmath{\*\moMath{\munknown x}}{\mmathPict{\pict\pngblip ",
            _PNG_HEX,
            rb"}}}}",
        ]
    )

    valid_blocks = RtfModel().predict(BytesIO(valid))[0]
    invalid_blocks = RtfModel().predict(BytesIO(invalid))[0]

    assert [block["type"] for block in valid_blocks] == [BlockType.EQUATION]
    assert [block["type"] for block in invalid_blocks] == [BlockType.IMAGE]


def test_rtf_unknown_destination_and_unbalanced_tail_recover_visible_text() -> None:
    """Verification Unknown ignorable destination Not leaked, missing trailing brackets remain Recovered text."""
    source = rb"{\rtf1\ansi{\*\unknown hidden}\pard visible\par"

    pages = RtfModel().predict(BytesIO(source))

    assert pages == [[{"type": BlockType.TEXT, "content": inline("visible")}]]


def test_rtf_source_html_is_rendered_as_inert_text() -> None:
    """Verify that the active HTML appearance in the source document does not go into HTML renderer DOM."""
    middle, _ = analyze_native_test_document(
        b"{\\rtf1\\ansi visible <script>alert(1)</script> and x < y\\par}", file_suffix="rtf"
    )

    html = render_html(middle, standalone=False)
    markdown = render_markdown(middle)

    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in markdown


def test_rtf_literal_tag_protocol_remains_inert_text() -> None:
    """To verify the original text of ordinary RTF label appearance, only TextSpan is generated, and formulas or links cannot be injected."""
    source = (
        rb"{\rtf1\ansi literal <eq>x</eq> and "
        rb"<hyperlink><text>click</text><url>javascript:alert(1)</url></hyperlink>\par}"
    )

    pages = RtfModel().predict(BytesIO(source))
    middle, _ = analyze_native_test_document(source, file_suffix="rtf")
    markdown = render_markdown(middle)

    content = pages[0][0]["content"]
    assert "<eq>x</eq>" in inline_text(content)
    assert "<hyperlink>" in inline_text(content)
    assert all(span.get("type") == "text" for span in content)
    assert "](javascript:alert(1))" not in markdown


def test_rtf_footnote_literal_tag_protocol_remains_inert_text() -> None:
    """Verify that the original text of RTF footnote label appearance only generates TextSpan, and cannot inject formulas or links."""
    source = (
        rb"{\rtf1\ansi Body{\footnote <eq>x</eq> and "
        rb"<hyperlink><text>click</text><url>javascript:alert(1)</url></hyperlink>}\par}"
    )

    middle, model = analyze_native_test_document(source, file_suffix="rtf")
    footnote = next(block for block in model.pages[0] if block.get("type") == BlockType.PAGE_FOOTNOTE)
    markdown = render_markdown(middle)
    docx = render_docx(middle)
    with ZipFile(BytesIO(docx)) as package:
        relationships = package.read("word/_rels/document.xml.rels").decode("utf-8")

    assert "<eq>x</eq>" in inline_text(footnote["content"])
    assert "<hyperlink>" in inline_text(footnote["content"])
    assert all(span.get("type") == "text" for span in footnote["content"])
    assert "](javascript:alert(1))" not in markdown
    assert "javascript:alert(1)" not in relationships


def test_rtf_annotation_preserves_comment_as_page_footnote() -> None:
    """Verify that annotation text goes into the footer block and does not generate spurious text footnote references."""
    source = rb"{\rtf1\ansi Before {\*\annotation Review comment} After\par}"

    pages = RtfModel().predict(BytesIO(source))

    assert pages[0][0]["type"] == BlockType.TEXT
    assert inline_text(pages[0][0]["content"]).split() == ["Before", "After"]
    assert "[1]" not in inline_text(pages[0][0]["content"])
    assert pages[0][1] == {"type": BlockType.PAGE_FOOTNOTE, "content": inline("[1] Review comment")}


def test_rtf_hidden_annotation_preserves_body_and_suppresses_metadata() -> None:
    """Verifying hidden annotations restores the body text without leaking author and identification metadata."""
    source = (
        rb"{\rtf1\ansi Body {\v {\*\atnauthor Alice}{\*\atnid AB}\chatn"
        rb"{\*\annotation Review <eq>x</eq> comment}} end\par}"
    )

    pages = RtfModel().predict(BytesIO(source))

    footnote = next(block for block in pages[0] if block.get("type") == BlockType.PAGE_FOOTNOTE)
    assert "Review <eq>x</eq> comment" in inline_text(footnote["content"])
    assert "Alice" not in inline_text(footnote["content"])
    assert "AB" not in inline_text(footnote["content"])
    assert all(span.get("type") == "text" for span in footnote["content"])


def test_rtf_equation_only_paragraph_preserves_note_reference() -> None:
    """Verify that pure formula paragraphs still retain footnote references and do not generate orphan footnote text."""
    source = rb"{\rtf1\ansi\pard {\mmath{\*\moMath{\mr x}}}{\footnote Foot.}\par}"

    pages = RtfModel().predict(BytesIO(source))

    assert pages[0][0]["type"] == BlockType.TEXT
    assert any(span.get("type") == "equation_inline" and span.get("content") == "x" for span in pages[0][0]["content"])
    assert "[1]" in inline_text(pages[0][0]["content"])
    assert pages[0][1] == {"type": BlockType.PAGE_FOOTNOTE, "content": inline("[1] Foot.")}


def test_rtf_object_keeps_safe_result_and_suppresses_objdata() -> None:
    """Verify that the object payload is not executed and is not leaked, but the explicit result text is still recoverable."""
    pages = RtfModel().predict(BytesIO(rb"{\rtf1 before {\object{\*\objdata 41424344}{\result Visible object}} after\par}"))

    assert pages == [[{"type": BlockType.TEXT, "content": inline("before Visible object after")}]]


def test_rtf_malformed_vector_picture_is_locally_dropped() -> None:
    """Verify that the bin bytes masquerading as WMF do not generate a placeholder image or corrupt the surrounding text."""
    pages = RtfModel().predict(BytesIO(rb"{\rtf1 before {\pict\wmetafile8\bin5 abcde} after\par}"))

    assert pages == [
        [
            {"type": BlockType.TEXT, "content": inline("before")},
            {"type": BlockType.TEXT, "content": inline("after")},
        ]
    ]


def test_rtf_valid_empty_and_invalid_header_have_distinct_results() -> None:
    """Legally empty RTF returns an empty logical page, non-RTF input returns a stable malformed error."""
    assert RtfModel().predict(BytesIO(rb"{\rtf1}")) == [[]]
    with pytest.raises(LegacyOfficeMalformedError, match="not an RTF"):
        RtfModel().predict(BytesIO(b"plain text"))


def test_rtf_resource_limits_are_fixed_and_non_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verification hard failed for token, group, input, asset, grid and nested-table caps."""
    monkeypatch.setattr(lexer_module, "MAX_RECORDS", 3)
    with pytest.raises(LegacyOfficeResourceLimitError, match="max_tokens"):
        list(RtfLexer(rb"{\rtf1 text}"))

    monkeypatch.setattr(lexer_module, "MAX_RECORDS", 16_000_000)
    with pytest.raises(LegacyOfficeResourceLimitError, match="max_group_depth"):
        list(RtfLexer(b"{" * 257 + b"}" * 257))

    monkeypatch.setattr(parser_module, "MAX_RTF_BYTES", 8)
    with pytest.raises(LegacyOfficeResourceLimitError, match="max_bytes"):
        RtfModel().predict(BytesIO(rb"{\rtf1 too long}"))

    monkeypatch.setattr(parser_module, "MAX_RTF_BYTES", 128 * 1024 * 1024)
    monkeypatch.setattr(parser_module, "MAX_ASSET_TOTAL_BYTES", 1)
    with pytest.raises(LegacyOfficeResourceLimitError, match="max_asset_total_bytes"):
        RtfModel().predict(BytesIO(rb"{\rtf1{\pict\pngblip 89504e47}}"))

    monkeypatch.setattr(parser_module, "MAX_ASSET_TOTAL_BYTES", 128 * 1024 * 1024)
    monkeypatch.setattr(parser_module, "MAX_GRID_SLOTS", 1)
    with pytest.raises(LegacyOfficeResourceLimitError, match="max_grid_slots"):
        RtfModel().predict(BytesIO(rb"{\rtf1\trowd\cellx1\cellx2 A\cell B\cell\row}"))

    monkeypatch.setattr(parser_module, "MAX_GRID_SLOTS", 4_000_000)
    with pytest.raises(LegacyOfficeResourceLimitError, match="max_table_depth"):
        RtfModel().predict(BytesIO(rb"{\rtf1\pard\intbl\itap5 nested\par}"))


def test_rtf_cell_definitions_enforce_budget_before_row_materialization(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that cellx executes the RTF dedicated cell budget during the definition list growth phase."""
    monkeypatch.setattr(parser_module, "MAX_RTF_TABLE_CELLS", 3)
    source = rb"{\rtf1\trowd\cellx1\cellx2\cellx3\cellx4}"

    with pytest.raises(LegacyOfficeResourceLimitError, match="max_grid_slots=3"):
        RtfModel().predict(BytesIO(source))


def test_rtf_prelude_nested_groups_do_not_materialize_overlapping_slices() -> None:
    """Verification prelude Peak memory remains a constant multiple of the input size when scanning deep group."""
    payload_size = 256 * 1024
    depth = 64
    source = b"{\\rtf1" + b"{" * depth + b"x" * payload_size + b"}" * depth + b"}"

    tracemalloc.start()
    try:
        parser_module.parse_rtf_prelude(source)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak_bytes < len(source) * 4


def test_rtf_runtime_has_no_anydoc_dependency() -> None:
    """Verify that dependency manifests, source imports, and lazy model loads do not contain anydoc."""
    assert "firecrawl-anydoc" not in (_PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    assert all(
        "import anydoc" not in path.read_text(encoding="utf-8") for path in (_PROJECT_ROOT / "src" / "docvortex").rglob("*.py")
    )
    script = "\n".join(
        [
            "import sys",
            "from docvortex.analyzers.native import RtfModel",
            "assert 'docvortex.analyzers.native.office.rtf.converter' not in sys.modules",
            "pages = RtfModel().predict(__import__('io').BytesIO(b'{\\\\rtf1 ok}'))",
            "assert pages == [[{'type': 'text', 'content': [{'type': 'text', 'content': 'ok'}]}]]",
            "assert 'anydoc' not in sys.modules",
        ]
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
