"""跨页表格延续文本与 caption 的轻量判定规则。"""

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

# 前缀仅供已归组 caption 使用，独立维护以免扩大正文提升范围。
_CONTINUATION_PREFIX_MARKERS = (
    "续表",
    "续上表",
    "continued",
    "cont.",
    "cont’d",
)


def _matches_continuation_caption_prefix(text: str) -> bool:
    """仅匹配 caption 的续表前缀，英文标记后不得紧接字母。"""
    continuation_text = full_to_half((text or "").strip()).lower()
    for marker in _CONTINUATION_PREFIX_MARKERS:
        if not continuation_text.startswith(marker):
            continue
        # 中文标记允许紧接标题，英文标记保留词边界，均不解析表号。
        if marker[0].isascii() and len(continuation_text) > len(marker) and continuation_text[len(marker)].isalpha():
            continue
        return True
    return False


def is_table_continuation_text(text: str) -> bool:
    """判断文本是否表达续表语义，供表格归组和跨页合并共同复用。"""
    continuation_text = full_to_half((text or "").strip()).lower()
    if not continuation_text:
        return False

    return any(
        _matches_continuation_end_marker(continuation_text, marker.lower()) for marker in CONTINUATION_END_MARKERS
    ) or any(marker.lower() in continuation_text for marker in CONTINUATION_INLINE_MARKERS)


def _matches_continuation_end_marker(text: str, marker: str) -> bool:
    """判断续表后缀是否按词边界命中，避免 discontinued 误命中 continued。"""
    if not text.endswith(marker):
        return False

    if marker == "continued":
        marker_start = len(text) - len(marker)
        return marker_start == 0 or not text[marker_start - 1].isalpha()

    return True
