"""Share textual evidence of figure annotations; use the same judgment for independent figure titles and text quotations, and do not import them across fields."""

from __future__ import annotations

import re
import unicodedata


_IDENTIFIER_PATTERN = (
    r"(?:"
    r"(?:[A-Z]\s*[.-]?\s*)?\d+(?:\s*[-./–—]\s*[A-Z]?\d+)*[A-Z]?"
    r"|[IVXLCDM]+"
    r"|[零〇一二三四五六七八九十百千两]+"
    r")"
)
_ENGLISH_CAPTION_RE = re.compile(
    rf"^\s*(?:fig(?:ure)?|tab(?:le)?|alg(?:orithm)?|listing|chart|scheme|diagram)(?=[.\s0-9])\.?(?:\s*)"
    rf"(?P<identifier>{_IDENTIFIER_PATTERN})(?P<tail>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_CHINESE_CAPTION_RE = re.compile(
    rf"^\s*(?:程序清单|图表|表格|算法|图|表)\s*"
    rf"(?P<identifier>{_IDENTIFIER_PATTERN})(?P<tail>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_ENGLISH_REFERENCE_TAIL_RE = re.compile(
    r"^(?:[,;]|and\b|or\b|&|shows?\b|illustrates?\b|presents?\b|depicts?\b|"
    r"demonstrates?\b|lists?\b|summari[sz]es?\b|reports?\b|compares?\b|"
    r"provides?\b|is\b|are\b|was\b|were\b|has\b|have\b|can\b)",
    re.IGNORECASE,
)
_CHINESE_REFERENCE_TAIL_RE = re.compile(
    r"^(?:[,;，；、]|和|及|与|为|是|展示|显示|给出|说明|列出|分别|可见|表明|描述|呈现|汇总|所示|中(?:的|为)?)",
)
_SUBFIGURE_REFERENCE_TAIL_RE = re.compile(
    r"^[（(][^）)]+[）)]\s*(?:[、,，]|和|及|与)",
)


def _normalize_annotation_text(text: str) -> str:
    """Uniform full-width characters and compatible with Roman numerals, retain the original text only for rule judgment."""

    return unicodedata.normalize("NFKC", text).strip()


def _caption_tail_is_reference(tail: str) -> bool:
    """Exclude quotations that are followed by descriptive predicates, parallel numbers, or text connectives immediately after the number."""

    stripped = tail.lstrip()
    if not stripped:
        return False
    if _SUBFIGURE_REFERENCE_TAIL_RE.match(stripped):
        return True
    if stripped[0] in ".:：-–—()[]（）":
        return False
    if _ENGLISH_REFERENCE_TAIL_RE.match(stripped) or _CHINESE_REFERENCE_TAIL_RE.match(stripped):
        return True
    return stripped[0].isascii() and stripped[0].isalpha() and stripped[0].islower()


def _is_strong_caption_text(text: str) -> bool:
    """Determine whether the text begins with a numbered Chinese and English strong chart title tag."""

    normalized = _normalize_annotation_text(text)
    match = _ENGLISH_CAPTION_RE.match(normalized) or _CHINESE_CAPTION_RE.match(normalized)
    return match is not None and not _caption_tail_is_reference(match.group("tail"))


def _caption_identifier(text: str) -> str | None:
    """Returns the normalized number shared by Chinese and English strong chart titles."""

    normalized = _normalize_annotation_text(text)
    match = _ENGLISH_CAPTION_RE.match(normalized) or _CHINESE_CAPTION_RE.match(normalized)
    return (
        unicodedata.normalize(
            "NFKC",
            match.group("identifier"),
        ).casefold()
        if match is not None
        else None
    )
