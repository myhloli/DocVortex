from __future__ import annotations

import math
from typing import Any

import pytest

from docvortex.analyzers.native.pdf import models, native_text


def _span(
    text: str,
    bbox: tuple[float, float, float, float],
    angle_degrees: float,
    *,
    chars: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Constructs a minimal pdftext span containing only orientation, text, and bbox."""

    return {
        "text": text,
        "bbox": bbox,
        "rotation": math.radians(angle_degrees),
        "chars": chars or [],
    }


def _char(
    value: str,
    bbox: tuple[float, float, float, float],
) -> dict[str, Any]:
    """Constructs the minimum pdftext character used for character baseline orientation testing."""

    return {"char": value, "bbox": bbox}


def _line(
    spans: list[dict[str, Any]],
    angle_degrees: float,
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 100.0, 100.0),
) -> dict[str, Any]:
    """Construct the minimum pdftext line used to test mixed orientation splits."""

    return {
        "spans": spans,
        "bbox": bbox,
        "rotation": math.radians(angle_degrees),
    }


def test_inline_script_scale_cache_is_linear_and_local_to_each_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each round of font size calculation increases linearly with the number of lines, and subsequent rounds re-read the changed character font sizes."""

    lines = [
        models._LineItem(
            text="body",
            bbox=(10.0, index * 30.0, 30.0, index * 30.0 + 10.0),
            angle=0,
            source_index=index,
            visual_row_id=index,
            effective_height=10.0,
            chars=[{"char": "a", "font": {"size": 10.0}}],
        )
        for index in range(20)
    ]
    observed: list[float] = []
    original = native_text._native_typographic_scale

    def record_scale(line: models._LineItem) -> float:
        """Record actual font size calculations and verify that the cache does not cross line change boundaries."""

        value = original(line)
        observed.append(value)
        return value

    monkeypatch.setattr(native_text, "_native_typographic_scale", record_scale)
    native_text._merge_native_inline_scripts(lines, (100.0, 700.0))
    assert observed == [10.0] * 20
    lines[0].chars[0]["font"]["size"] = 12.0
    observed.clear()
    native_text._merge_native_inline_scripts(lines, (100.0, 700.0))
    assert observed == [12.0, *([10.0] * 19)]


def test_native_typographic_cache_skips_python_character_scan() -> None:
    """Rust The precalculated median font size is reused first, and the exception cache still falls back to the original implementation."""

    cached = models._LineItem("body", (0, 0, 10, 10), 0, 0, effective_height=10.0)
    cached.native_typographic_scale = 7.0
    assert native_text._native_typographic_scale(cached) == 7.0
    invalid = models._LineItem("body", (0, 0, 10, 10), 0, 1, effective_height=6.0)
    invalid.native_typographic_scale = float("nan")
    assert native_text._native_typographic_scale(invalid) == 6.0


def test_private_use_decorative_rule_becomes_axis_line() -> None:
    """Verify that repeated glyphs in the header wide private area no longer enter the text output."""

    decorative = models._LineItem(
        text="\ue123" * 24,
        bbox=(10.0, 12.0, 190.0, 16.0),
        angle=0,
        source_index=0,
    )
    body = models._LineItem(
        text="body",
        bbox=(10.0, 40.0, 80.0, 50.0),
        angle=0,
        source_index=1,
    )

    retained, rules = native_text._extract_decorative_text_rules(
        [decorative, body],
        (200.0, 200.0),
    )

    assert retained == [body]
    assert [(rule.orientation, rule.bbox) for rule in rules] == [("horizontal", decorative.bbox)]


def test_mixed_footer_and_315_degree_watermark_are_split_before_filtering() -> None:
    """Verify that span close to 315 degrees does not continue to borrow parent row 0 degree orientation with giant bbox."""

    pdf_line = _line(
        [
            _span("文档问题反馈: aw-document@example.com", (30.0, 90.0, 80.0, 98.0), 0.0),
            _span("通用测试文档", (5.0, 10.0, 25.0, 95.0), 315.000019),
            _span("\r\n", (26.0, 20.0, 26.0, 20.0), 0.0),
        ],
        0.0,
    )

    children = native_text._split_pdftext_line_by_rotation(pdf_line)
    items = native_text._build_native_line_items([pdf_line], (100.0, 100.0))

    assert [round(math.degrees(child["rotation"]), 6) for child in children] == [0.0, 315.000019]
    assert children[0]["bbox"] == (30.0, 90.0, 80.0, 98.0)
    assert children[1]["bbox"] == (5.0, 10.0, 25.0, 95.0)
    assert [(item.text, item.bbox, item.angle) for item in items] == [
        ("文档问题反馈: aw-document@example.com", (30.0, 90.0, 80.0, 98.0), 0)
    ]


def test_small_oblique_span_stays_inside_standard_direction_line() -> None:
    """Verify that faux italic span at about 19 degrees does not trigger directional splitting and follows parent row 0 degrees."""

    pdf_line = _line(
        [
            _span("normal ", (10.0, 20.0, 40.0, 30.0), 0.0),
            _span("oblique", (40.0, 18.0, 75.0, 30.0), 19.0),
        ],
        0.0,
    )

    children = native_text._split_pdftext_line_by_rotation(pdf_line)
    items = native_text._build_native_line_items([pdf_line], (100.0, 100.0))

    assert len(children) == 1
    assert math.degrees(children[0]["rotation"]) == pytest.approx(0.0)
    assert [(item.text, item.angle) for item in items] == [("normal oblique", 0)]


def test_sheared_horizontal_line_uses_char_baseline_and_splits_far_sidecar() -> None:
    """Verify that faux-italic bold lines can be recovered from the horizontal character baseline and continue to break up remote page numbers."""

    chars = [_char(value, (10.0 + index * 5.0, 80.0, 14.0 + index * 5.0, 88.0)) for index, value in enumerate("NOTICE")]
    chars.append(_char("2", (90.0, 80.2, 94.0, 88.2)))
    pdf_line = _line(
        [
            _span(
                "NOTICE 2",
                (10.0, 80.0, 94.0, 88.2),
                18.433,
                chars=chars,
            )
        ],
        18.433,
        bbox=(10.0, 80.0, 94.0, 88.2),
    )

    items = native_text._build_native_line_items([pdf_line], (100.0, 100.0))

    assert [(item.text, item.angle) for item in items] == [
        ("NOTICE", 0),
        ("2", 0),
    ]
    assert all(item.split_from_row for item in items)


def test_true_diagonal_char_baseline_is_not_recovered_as_horizontal() -> None:
    """Verify that true slanted character centers are not reverted to horizontal text due to character count and aspect ratio."""

    chars = [
        _char(value, (10.0 + index * 8.0, 70.0 - index * 6.0, 14.0 + index * 8.0, 78.0 - index * 6.0))
        for index, value in enumerate("WATERMARK")
    ]
    pdf_line = _line(
        [
            _span(
                "WATERMARK",
                (10.0, 22.0, 78.0, 78.0),
                315.0,
                chars=chars,
            )
        ],
        315.0,
        bbox=(10.0, 22.0, 78.0, 78.0),
    )

    assert native_text._build_native_line_items([pdf_line], (100.0, 100.0)) == []


def test_small_shear_formula_line_is_retained_as_formula_only() -> None:
    """Verify that small angle multi-baseline thick lines with math operators only go into the formula-only stream."""
    chars = [
        _char("x", (10.0, 20.0, 15.0, 28.0)),
        _char("=", (18.0, 24.0, 24.0, 30.0)),
    ]
    pdf_line = _line(
        [_span("x=", (10.0, 20.0, 24.0, 30.0), 10.0, chars=chars)],
        10.0,
        bbox=(10.0, 20.0, 24.0, 30.0),
    )

    items = native_text._build_native_line_items([pdf_line], (100.0, 100.0))

    assert [(item.text, item.angle, item.formula_candidate_only) for item in items] == [("x=", 0, True)]


def test_formula_only_rows_do_not_shift_existing_source_indices() -> None:
    """Verify that the new formula candidate uses the tail source and index without changing the existing natural text identity."""
    first = _line([_span("first", (10.0, 10.0, 35.0, 18.0), 0.0)], 0.0)
    formula = _line([_span("x=", (10.0, 20.0, 24.0, 30.0), 10.0)], 10.0)
    second = _line([_span("second", (10.0, 32.0, 40.0, 40.0), 0.0)], 0.0)

    items = native_text._build_native_line_items([first, formula, second], (100.0, 100.0))

    assert {item.text: item.source_index for item in items} == {"first": 0, "second": 1, "x=": 2}


def test_small_true_diagonal_without_formula_evidence_stays_rejected() -> None:
    """Verify that short italic text lacking formula evidence does not flow back into the text by formula-only."""
    chars = [
        _char(value, (10.0 + index * 5.0, 30.0 - index * 2.0, 14.0 + index * 5.0, 38.0 - index * 2.0))
        for index, value in enumerate("mark")
    ]
    pdf_line = _line(
        [_span("mark", (10.0, 24.0, 29.0, 38.0), 10.0, chars=chars)],
        10.0,
        bbox=(10.0, 24.0, 29.0, 38.0),
    )

    assert native_text._build_native_line_items([pdf_line], (100.0, 100.0)) == []


@pytest.mark.parametrize(
    ("line_angle", "page_rotation", "expected_angle"),
    [
        (0.0, 0, 0),
        (90.0, 0, 90),
        (270.0, 0, 270),
        (180.0, 0, None),
        (315.000019, 0, None),
        (180.0, 90, 270),
        (90.0, 90, None),
    ],
)
def test_only_supported_visual_line_directions_are_retained(
    line_angle: float,
    page_rotation: int,
    expected_angle: int | None,
) -> None:
    """Verify that the orientation whitelist takes effect after the page is rotated, and does not normalize diagonal rows to 0 degrees first."""

    pdf_line = _line([_span("value", (10.0, 20.0, 40.0, 30.0), line_angle)], line_angle)

    items = native_text._build_native_line_items(
        [pdf_line],
        (100.0, 100.0),
        page_rotation=page_rotation,
    )

    assert [item.angle for item in items] == ([] if expected_angle is None else [expected_angle])


def test_native_line_builder_can_opt_in_to_180_degree_visual_runs() -> None:
    """Verified that Low/TXT can explicitly extend the direction whitelist without changing the Flash default behavior."""

    pdf_line = _line(
        [_span("upside down", (10.0, 20.0, 60.0, 30.0), 180.0)],
        180.0,
        bbox=(10.0, 20.0, 60.0, 30.0),
    )

    default_items = native_text._build_native_line_items(
        [pdf_line],
        (100.0, 100.0),
    )
    low_txt_items = native_text._build_native_line_items(
        [pdf_line],
        (100.0, 100.0),
        supported_angles=(0.0, 90.0, 180.0, 270.0),
    )

    assert default_items == []
    assert [(item.text, item.angle) for item in low_txt_items] == [("upside down", 180)]


@pytest.mark.parametrize(
    "separator",
    [
        "\u00a0",
        "\u1680",
        "\u2000",
        "\u2001",
        "\u2002",
        "\u2003",
        "\u2004",
        "\u2005",
        "\u2006",
        "\u2007",
        "\u2008",
        "\u2009",
        "\u200a",
        "\u202f",
        "\u205f",
        "\u3000",
    ],
)
def test_pdf_unicode_separator_spaces_are_normalized_to_ascii(separator: str) -> None:
    """Verify that all Unicode Zs typographic spaces are converted to normal ASCII spaces."""

    assert (
        native_text._sanitize_pdf_control_text(
            f"left{separator}right",
            preserve_newlines=True,
        )
        == "left right"
    )


def test_pdf_unicode_line_separators_follow_newline_policy() -> None:
    """Verify that NEXT LINE, line delimiters, and segment delimiters follow the physical line break retention policy."""

    content = "first\u0085second\u2028third\u2029fourth"

    assert (
        native_text._sanitize_pdf_control_text(
            content,
            preserve_newlines=True,
        )
        == "first\nsecond\nthird\nfourth"
    )
    assert (
        native_text._sanitize_pdf_control_text(
            content,
            preserve_newlines=False,
        )
        == "firstsecondthirdfourth"
    )


def test_pdf_safe_invisible_and_control_characters_are_removed_idempotently() -> None:
    """Verify that zero-width characters with no body semantics and C0/C1 control characters are stably removed."""

    content = "A\u200bB\u2060C\ufeffD\x00E\x07F\x7fG\x80H\x9fI"
    normalized = native_text._sanitize_pdf_control_text(
        content,
        preserve_newlines=True,
    )

    assert normalized == "ABCDEFGHI"
    assert (
        native_text._sanitize_pdf_control_text(
            normalized,
            preserve_newlines=True,
        )
        == normalized
    )


def test_pdf_soft_hyphens_keep_only_latin_line_end_breaks() -> None:
    """Verify that two types of PDF soft-hyphenation words are converted to ASCII hyphen only at the end of lines with Latin letters."""

    assert native_text._normalize_native_run_text("inter\u00ad") == "inter-"
    assert native_text._normalize_native_run_text("co\u00adoperate") == "cooperate"
    assert native_text._normalize_native_run_text("word\x02") == "word-"
    assert native_text._normalize_native_run_text("A\x02B") == "AB"


def test_pdf_semantic_joiners_and_decode_markers_are_preserved() -> None:
    """Verify that language connectors, private area glyphs, and decoding placeholders are not silently removed by universal cleanup."""

    content = "a\u200cb\u200dc\uf8f1d\ufffde"

    assert (
        native_text._sanitize_pdf_control_text(
            content,
            preserve_newlines=True,
        )
        == content
    )
