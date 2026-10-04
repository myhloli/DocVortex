"""Cross-page table continuation text and lightweight decision rules of caption."""

from ...foundation._text import full_to_half

CONTINUATION_END_MARKERS = [
    "(续)",
    "(续表)",
    "(续上表)",
    "(continued)",
    "(cont.)",
    "(cont’d)",
    "(…continued)",
    "continued",
    "续表",
]

CONTINUATION_INLINE_MARKERS = [
    "(continued)",
]


def is_table_continuation_text(text: str) -> bool:
    """Determine whether the text expresses table continuation semantics for table grouping and cross-page merging for reuse."""
    continuation_text = full_to_half((text or "").strip()).lower()
    if not continuation_text:
        return False

    return any(
        _matches_continuation_end_marker(continuation_text, marker.lower()) for marker in CONTINUATION_END_MARKERS
    ) or any(marker.lower() in continuation_text for marker in CONTINUATION_INLINE_MARKERS)


def _matches_continuation_end_marker(text: str, marker: str) -> bool:
    """Determine whether the continuation table suffix is hit according to word boundaries to avoid discontinued accidentally hitting continued."""
    if not text.endswith(marker):
        return False

    if marker == "continued":
        marker_start = len(text) - len(marker)
        return marker_start == 0 or not text[marker_start - 1].isalpha()

    return True
