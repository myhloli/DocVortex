from __future__ import annotations

import pytest
from _span_test_utils import inline as _inline

from docvortex.content.inline import inline_plain_text
from docvortex.render._internal.common.index import looks_like_index_page_token, strip_index_page_tail
from docvortex.render._internal.common.list_items import (
    has_markdown_unordered_marker,
    parse_list_item_marker,
    reference_list_needs_bullets,
)
from docvortex.schema import ListBlock, RefTextBlock, TextBlock


@pytest.mark.parametrize(
    ("content", "marker", "body", "kind", "value", "ordered_style"),
    [
        ("- item", "-", "item", "unordered", None, None),
        ("* item", "*", "item", "unordered", None, None),
        ("+ item", "+", "item", "unordered", None, None),
        ("12. item", "12.", "item", "ordered", 12, "decimal"),
        ("a. item", "a.", "item", "ordered", 1, "lower-alpha"),
        ("A. item", "A.", "item", "ordered", 1, "upper-alpha"),
        ("i. item", "i.", "item", "ordered", 1, "lower-roman"),
        ("I. item", "I.", "item", "ordered", 1, "upper-roman"),
        ("iv. item", "iv.", "item", "ordered", 4, "lower-roman"),
        ("XII. item", "XII.", "item", "ordered", 12, "upper-roman"),
        ("12) item", "12)", "item", "explicit", None, None),
        ("(12) item", "(12)", "item", "explicit", None, None),
        ("(12. item", "(12.", "item", "explicit", None, None),
        ("a) item", "a)", "item", "explicit", None, None),
        ("[12] item", "[12]", "item", "explicit", None, None),
        ("[x] done", "[x]", "done", "explicit", None, None),
        ("[ ] pending", "[ ]", "pending", "explicit", None, None),
        ("plain item", None, "plain item", "none", None, None),
        ("-without-space", None, "-without-space", "none", None, None),
    ],
)
def test_parse_list_item_marker_classifies_supported_styles(
    content: str,
    marker: str | None,
    body: str,
    kind: str,
    value: int | None,
    ordered_style: str | None,
) -> None:
    """Validate shared parser to differentiate between native lists and numbering styles that require an explicit marker."""
    item = parse_list_item_marker(_inline(content))

    assert item.marker == marker
    assert inline_plain_text(item.body) == body
    assert item.kind == kind
    assert item.value == value
    assert item.ordered_style == ordered_style


@pytest.mark.parametrize("content", ["  2. item", "\t- item", "- \ncontinued", "  plain item"])
def test_parse_list_item_marker_preserves_reconstructable_whitespace(content: str) -> None:
    """Verify that parser saves leading and delimiting whitespace separately and can reconstruct the original content without loss."""
    item = parse_list_item_marker(_inline(content))
    reconstructed = item.leading
    if item.marker is not None:
        reconstructed += item.marker + item.separator
    reconstructed += inline_plain_text(item.body)

    assert reconstructed == content


def test_single_roman_letters_use_stable_roman_precedence() -> None:
    """Verify that Roman characters with the same shape as alphabetical numbers are always interpreted as Roman numerals first."""
    assert parse_list_item_marker(_inline("v. item")).ordered_style == "lower-roman"
    assert parse_list_item_marker(_inline("v. item")).value == 5
    assert parse_list_item_marker(_inline("c. item")).ordered_style == "lower-roman"
    assert parse_list_item_marker(_inline("c. item")).value == 100
    assert parse_list_item_marker(_inline("z. item")).ordered_style == "lower-alpha"
    assert parse_list_item_marker(_inline("z. item")).value == 26


@pytest.mark.parametrize(
    "content",
    [
        f"{'9' * 5000}. item",
        "1000001. item",
        "01. item",
        "００１. item",
        "IIII. item",
        f"{'M' * 5000}. item",
    ],
)
def test_unbounded_or_invalid_ordered_markers_degrade_to_explicit(content: str) -> None:
    """Verify that overlong, out-of-bounds, or non-canonical numbers do not trigger a large integer exception and leave marker as is."""
    item = parse_list_item_marker(_inline(content))

    assert item.kind == "explicit"
    assert item.marker is not None
    assert item.value is None
    assert item.ordered_style is None


def test_markdown_existing_bullet_detection_remains_hyphen_only() -> None:
    """Verification reference supplement bullet only avoids existing dashes and maintains Markdown historical output."""
    assert has_markdown_unordered_marker(_inline("  - existing"))
    assert not has_markdown_unordered_marker(_inline("* existing"))
    assert not has_markdown_unordered_marker(_inline("+ existing"))
    assert not has_markdown_unordered_marker(_inline("-without-space"))
    assert not has_markdown_unordered_marker(_inline("-\ncontinued"))


def _reference_list(*children: TextBlock | RefTextBlock | ListBlock) -> ListBlock:
    """Construct a reference list for strict majority determination."""
    return ListBlock(type="list", sub_type="ref_text", content=list(children))


def test_reference_list_bullet_rule_uses_visible_direct_item_strict_majority() -> None:
    """Verify that rich text visible numbers, empty items, and nested lists adhere to the established strict majority rule."""
    nested = _reference_list(RefTextBlock(type="ref_text", content=_inline("Author nested")))
    numbered_majority = _reference_list(
        RefTextBlock(
            type="ref_text",
            content=[
                {"type": "text", "content": "[1]", "styles": ["bold"]},
                {"type": "text", "content": " first"},
            ],
        ),
        RefTextBlock(type="ref_text", content=_inline("missing marker")),
        TextBlock(type="text", content=_inline("3) third")),
        RefTextBlock(type="ref_text", content=[]),
        nested,
    )
    tied = _reference_list(
        RefTextBlock(type="ref_text", content=_inline("[1] first")),
        RefTextBlock(type="ref_text", content=_inline("Author A")),
    )

    assert not reference_list_needs_bullets(numbered_majority)
    assert reference_list_needs_bullets(tied)
    assert not reference_list_needs_bullets(ListBlock(type="list", sub_type="text", content=tied.content))


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("Title", "Title"),
        ("Title\t12", "Title"),
        ('Title\t<text style="bold">IV</text>', "Title"),
        ("Part\tSection\tA", "Part Section"),
        ("Title\tAppendix", "Title Appendix"),
        ("Title\tA2", "Title A2"),
    ],
)
def test_strip_index_page_tail_uses_visible_tail_token(content: str, expected: str) -> None:
    """Verify that the table of contents removes only trusted last page numbers and converts retained tab to spaces."""
    spans = (
        [
            {"type": "text", "content": "Title\t"},
            {"type": "text", "content": "IV", "styles": ["bold"]},
        ]
        if "<text" in content
        else _inline(content)
    )
    assert inline_plain_text(strip_index_page_tail(spans)) == expected


@pytest.mark.parametrize("content", ["1", "１２", "iv", "XII", "a", "Z"])
def test_index_page_token_accepts_supported_forms(content: str) -> None:
    """Verify that numbers, Roman numerals and single letters can be used as catalog page numbers token."""
    assert looks_like_index_page_token(content)


@pytest.mark.parametrize("content", ["", "AB", "A2", "appendix", "1234567890123"])
def test_index_page_token_rejects_ambiguous_forms(content: str) -> None:
    """Verify that null values, long values, and common words are not mistakenly deleted as table of contents page numbers."""
    assert not looks_like_index_page_token(content)
