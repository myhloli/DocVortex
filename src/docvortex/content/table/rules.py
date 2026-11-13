"""跨页表格延续文本与 caption 的轻量判定规则。"""

import re

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

_CONTINUATION_PREFIX_RE = re.compile(r"^(?:(?:续表|续上表)\s*|continued\s+table\s+)(?=[a-z]?\d)")


def is_table_continuation_text(text: str) -> bool:
    """判断后缀、行内或带编号前缀是否表达续表语义。"""
    continuation_text = full_to_half((text or "").strip()).lower()
    if not continuation_text:
        return False

    return (
        any(_matches_continuation_end_marker(continuation_text, marker.lower()) for marker in CONTINUATION_END_MARKERS)
        or any(marker.lower() in continuation_text for marker in CONTINUATION_INLINE_MARKERS)
        or _CONTINUATION_PREFIX_RE.match(continuation_text) is not None
    )


def _matches_continuation_end_marker(text: str, marker: str) -> bool:
    """判断续表后缀是否按词边界命中，避免 discontinued 误命中 continued。"""
    if not text.endswith(marker):
        return False

    if marker == "continued":
        marker_start = len(text) - len(marker)
        return marker_start == 0 or not text[marker_start - 1].isalpha()

    return True
