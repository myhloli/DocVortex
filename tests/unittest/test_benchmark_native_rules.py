"""Constrain native tables, panels, and member ordering rules with deformed positive examples and missing-evidence counterexamples."""

from dataclasses import replace
import pytest
from docvortex.analyzers.native.pdf._table_recovery.banded_numeric import build_banded_numeric_candidate
from docvortex.analyzers.native.pdf._table_recovery.contracts import NativeTableInput, NativeTableRule
from docvortex.analyzers.native.pdf._table_recovery.text import build_native_table_text
from docvortex.analyzers.native.pdf.models import _LineItem
from docvortex.analyzers.native.pdf.title_analysis.panels import classify_panel_titles
from docvortex.analyzers.native.pdf.panel_order import numbered_step_member_groups
from test_native_pdf_table import _char_items
from docvortex.analyzers.native.pdf.graphics import _image_members_to_content


def _numeric_table(scale: float, offset: float) -> NativeTableInput:
    """Construct a few-line table with two columns and three data rows. Panning and scaling should not change the structure judgment."""
    entries = [("Material", (4, 5, 45, 13)), ("Value", (80, 5, 105, 13))]
    for row, value in enumerate(["10", "20", "30"]):
        y = 23 + row * 18
        entries.extend([(f"Mineral {row}", (4, y, 44, y + 8)), (value, (80, y, 92, y + 8))])
    entries = [(text, tuple(offset + v * scale for v in box)) for text, box in entries]
    end = offset + 120 * scale
    bottom = offset + 80 * scale
    return NativeTableInput(
        (offset, offset, end, bottom),
        (end + 10, bottom + 10),
        0,
        _char_items(entries),
        (
            NativeTableRule((offset, offset, end, offset + scale), scale, "horizontal"),
            NativeTableRule((offset, bottom - scale, end, bottom), scale, "horizontal"),
        ),
    )


@pytest.mark.parametrize("scale,offset", [(1, 0), (0.6, 19), (1.8, 31)])
def test_sparse_numeric_table_is_scale_and_position_independent(scale, offset):
    """Repeating numerical columns and physical outer lines together prove four rows and two columns without relying on fixed coordinates."""
    table = _numeric_table(scale, offset)
    text = build_native_table_text(table)
    candidate = build_banded_numeric_candidate(table, text, {})
    assert candidate is not None
    assert (candidate.rows, candidate.cols) == (4, 2)
    assert [cell.content for cell in candidate.cells if cell.col == 1] == ["Value", "10", "20", "30"]


def test_sparse_numeric_table_requires_physical_support():
    """Paragraphs without underlining or background color support cannot be restored to tables based on numerical columns alone."""
    table = replace(_numeric_table(1, 0), drawing_lines=())
    assert build_banded_numeric_candidate(table, build_native_table_text(table), {}) is None


def test_sparse_numeric_table_rejects_crossing_glyph():
    """Complete large glyphs that cross column boundaries in the text cannot be split into two cells by new candidates."""
    table = _numeric_table(1, 0)
    chars = (*table.chars, {"char": "W", "bbox": (40, 23, 84, 31), "char_idx": len(table.chars)})
    table = replace(table, chars=chars)
    assert build_banded_numeric_candidate(table, build_native_table_text(table), {}) is None


def _panel_lines(body_height: float = 10) -> list[_LineItem]:
    """Construct two repeated columns of equation title and introduction with different font sizes, without giving a specific page name."""
    lines = []
    for column in range(2):
        x = 20 + 220 * column
        for text, y, height in [("Panel subject", 20, 14), ("Several words describe the subject", 52, body_height)]:
            lines.append(
                _LineItem(
                    text,
                    (x, y, x + 140, y + height),
                    0,
                    len(lines),
                    effective_height=height,
                    font_signature=("Arial", 0),
                    font_coverage=1,
                )
            )
    return lines


def test_parallel_panel_titles_require_typographic_transition():
    """The title and small font introduction of the adjacent full panel constitute positive evidence."""
    lines = _panel_lines()
    classify_panel_titles(lines, [], 500)
    assert [line.semantic_type for line in lines] == ["paragraph_title", None, "paragraph_title", None]


@pytest.mark.parametrize("kind", ["same_body_scale", "single_panel", "different_style"])
def test_panel_rule_rejects_unverified_parallel_prose(kind):
    """Text with the same font size, isolated tags, or rows of different styles cannot be promoted to titles through repeated column layout."""
    lines = _panel_lines(14 if kind == "same_body_scale" else 10)
    if kind == "single_panel":
        lines = lines[:2]
    if kind == "different_style":
        lines[2].font_signature = ("Other", 0)
    classify_panel_titles(lines, [], 500)
    assert all(line.semantic_type is None for line in lines)


def test_step_member_group_requires_consecutive_children():
    """The absence of an intermediate child does not claim to stabilize the letter sequence, nor does it change the global reading order."""
    blocks = [
        {"type": "text", "content": value, "bbox": (left, top, left + 130, top + 10)}
        for value, left, top in [
            ("1. Parent", 10, 10),
            ("a. Child", 25, 20),
            ("c. Child", 25, 30),
            ("d. Child", 25, 40),
            ("2. Next", 10, 50),
        ]
    ]
    assert numbered_step_member_groups(blocks, set()) == []


def test_rotated_axis_keeps_left_to_right_order_for_unequal_label_lengths():
    """Long and short labels that share a common axis are arranged according to the abscissa, and cannot form different years or category groups based on text length."""
    members = [_LineItem(str(i), (10, 10 + i * 8, 15, 16 + i * 8), 0, i, effective_height=6) for i in range(8)]
    for column, (text, length) in enumerate([("Longest category", 70), ("Short", 25), ("Middle label", 50)]):
        members.append(
            _LineItem(text, (50 + column * 30, 90, 56 + column * 30, 90 + length), 270, 8 + column, effective_height=6)
        )
    content = _image_members_to_content(members, (200, 200))
    assert content.index("Longest category") < content.index("Short") < content.index("Middle label")
