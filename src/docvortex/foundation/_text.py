"""Text character normalization and line wrapping join rules shared across models."""

import re
import unicodedata
from collections.abc import Sequence

from .language import remove_invalid_surrogates

# Use fixed code point intervals to cover extended kanji, kana, and Chinese and Japanese punctuation to avoid changing results with different Unicode versions of Python.
_UNSPACED_RANGES = (
    (0x3000, 0x303F),
    (0x3040, 0x30FF),
    (0x31F0, 0x31FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0xFF61, 0xFF9F),
    (0x1AFF0, 0x1AFFF),
    (0x1B000, 0x1B16F),
    (0x20000, 0x2EE5F),
    (0x2F800, 0x2FA1F),
    (0x30000, 0x323AF),
)
_CJK_PUNCTUATION = frozenset("，。！？；：、（）【】《》〈〉「」『』〔〕［］｛｝")
_CLOSING_PUNCTUATION = frozenset(",.;:!?%")
# Explicit compound word prefixes and connectives are only used to conservatively retain hard hyphens and do not undertake language recognition or dictionary segmentation.
_COMPOUND_PREFIXES = frozenset({"open", "non", "self", "cross", "anti", "pre", "post", "co", "semi", "multi", "quasi"})
_COMPOUND_CONNECTORS = frozenset({"of", "the", "to", "and", "in", "for", "on", "by", "with"})
# Only existing inline style tags are projected, and any HTML or formulas are not interpreted as overridable content.
_INLINE_STYLE_TAG_RE = re.compile(r"</?(?:sup|sub|b|strong|i|em|u|s|del|strike)\b[^>]*>", re.IGNORECASE)

# PDF When extracting text, English cross-line word segmentation may be encoded into a variety of hyphen characters.
# This is only used to determine the "end-of-line English word breaker", and does not extend to ordinary dashes such as en/em dash.
LINE_END_HYPHEN_CHARS = "-\u00ad\u2010\u2011\u2043"
LINE_END_HYPHEN_RE = re.compile(rf"[A-Za-z]+[{re.escape(LINE_END_HYPHEN_CHARS)}]\s*$")

# The URL candidate only allows RFC 3986 common ASCII characters to avoid swallowing the Chinese text into the link.
_URL_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:(?:https?|ftp)://|www\.)[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+",
    re.ASCII | re.IGNORECASE,
)
# When the next line itself begins with the full URL, the boundary must be preserved to prevent two independent links from being connected.
_URL_AT_LINE_START_RE = re.compile(
    r"(?:(?:https?|ftp)://|www\.)",
    re.ASCII | re.IGNORECASE,
)


def is_hyphen_at_line_end(line: str) -> bool:
    """Determine whether the text line ends with an English word's cross-line breaker.

    Only recognizes word segmentation scenarios where a letter is immediately followed by the end-of-line hyphen and does not handle intra-word hyphens or ordinary dashes.
    """
    return bool(LINE_END_HYPHEN_RE.search(line))


def _url_spans_line_boundary(previous_content: str, next_content: str) -> bool:
    """Determine whether there is a URL in the non-space candidate that strictly spans the current physical line boundary."""
    stripped_previous = previous_content.rstrip()
    stripped_next = next_content.lstrip()
    if not stripped_previous or not stripped_next:
        return False
    candidate = f"{stripped_previous}{stripped_next}"
    boundary = len(stripped_previous)
    return any(match.start() < boundary < match.end() for match in _URL_CANDIDATE_RE.finditer(candidate))


def resolve_text_line_boundary(
    previous_content: str,
    *,
    next_content: str,
) -> tuple[str, str]:
    """Returns the processed content of the previous line and the current physical line boundary separator.

    URL candidates that strictly span the boundary are connected directly, but spaces are preserved when the next line is itself a complete URL.
    Spaces are judged based on the current visible boundary, and the entire language is not detected. Kanji and kana are directly connected, and Korean and
    Spanish retains spaces between words; the established URL and English word breaking rules take precedence, and only the actual end-of-line word breaking characters are rewritten.
    """
    processed_content = remove_invalid_surrogates(previous_content).rstrip()
    if not processed_content:
        return "", ""
    stripped_next = remove_invalid_surrogates(next_content).lstrip()
    if not stripped_next:
        return processed_content, ""
    if _url_spans_line_boundary(processed_content, stripped_next):
        if _URL_AT_LINE_START_RE.match(stripped_next):
            return processed_content, " "
        return processed_content, ""
    if is_hyphen_at_line_end(processed_content):
        previous_word = re.search(r"([A-Za-z]+)[-\u00ad\u2010\u2011\u2043]$", processed_content)
        # The connectors of abbreviated compound words such as GLGE-difficult have semantic meaning and cannot be deleted as ordinary word breakers.
        acronym = previous_word is not None and len(previous_word[1]) > 1 and previous_word[1].isupper()
        hard_compound = (
            previous_word is not None
            and processed_content.endswith("-")
            and (
                previous_word[1].lower() in _COMPOUND_PREFIXES
                or (
                    previous_word[1].lower() in _COMPOUND_CONNECTORS
                    and previous_word.start() > 0
                    and processed_content[previous_word.start() - 1] == "-"
                )
            )
        )
        if stripped_next[0].islower() and not acronym and not hard_compound:
            return processed_content[:-1], ""
        return processed_content, ""
    if re.search(r"\d[-–]$", processed_content) and stripped_next[0].isdigit():
        return processed_content, ""
    previous_char = _boundary_character(processed_content, from_end=True)
    next_char = _boundary_character(stripped_next, from_end=False)
    if not previous_char or not next_char:
        return processed_content, ""
    if _is_unspaced_character(previous_char) or _is_unspaced_character(next_char):
        return processed_content, ""
    if unicodedata.category(previous_char) in {"Ps", "Pi"}:
        return processed_content, ""
    if unicodedata.category(next_char) in {"Pe", "Pf"} or next_char in _CLOSING_PUNCTUATION:
        return processed_content, ""
    return processed_content, " "


def _boundary_character(content: str, *, from_end: bool) -> str:
    """Read visible boundary characters, ignore style tags and attached combining characters, and do not rewrite the original text."""
    visible = _INLINE_STYLE_TAG_RE.sub("", content).strip()
    characters = reversed(visible) if from_end else iter(visible)
    return next((char for char in characters if unicodedata.category(char)[0] not in {"M", "C"}), "")


def _is_unspaced_character(char: str) -> bool:
    """Determine Chinese characters, kana, Chinese and Japanese punctuation, and Korean letters are not spaces-free text."""
    return char in _CJK_PUNCTUATION or any(start <= ord(char) <= end for start, end in _UNSPACED_RANGES)


def merge_text_line_contents(
    line_contents: Sequence[str],
) -> str:
    """Collapse of physical lines by cumulative text context, supporting URL continuous splicing spanning more than three lines."""

    normalized_lines = [cleaned for content in line_contents if (cleaned := remove_invalid_surrogates(str(content)).strip())]
    if not normalized_lines:
        return ""
    merged_content = normalized_lines[0]
    for current_line in normalized_lines[1:]:
        merged_content, separator = resolve_text_line_boundary(
            merged_content,
            next_content=current_line,
        )
        merged_content = f"{merged_content}{separator}{current_line}"
    return merged_content.strip()


def full_to_half_exclude_marks(text: str) -> str:
    """Convert full-width English letters and numbers to half-width form while retaining full-width punctuation."""
    result = []
    for char in text:
        code = ord(char)
        # Full-width letters and numbers (FF21-FF3A for A-Z, FF41-FF5A for a-z, FF10-FF19 for 0-9)
        if (0xFF21 <= code <= 0xFF3A) or (0xFF41 <= code <= 0xFF5A) or (0xFF10 <= code <= 0xFF19):
            result.append(chr(code - 0xFEE0))  # Shift to ASCII range
        else:
            result.append(char)
    return "".join(result)


def full_to_half(text: str) -> str:
    """Convert full-width ASCII letters, numbers and punctuation characters to half-width form."""
    result = []
    for char in text:
        code = ord(char)
        # Full-width letters, numbers and punctuation (FF01-FF5E)
        if 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))  # Shift to ASCII range
        else:
            result.append(char)
    return "".join(result)


def clean_isolated_formula(content: str) -> str:
    """Remove outer backslash brackets from inline formulas and clear leading and trailing whitespace."""
    latex = content[:]
    if latex.startswith("\\["):
        latex = latex[2:]
    if latex.endswith("\\]"):
        latex = latex[:-2]
    return latex.strip()


def normalize_formula_tag_content(tag_content: str) -> str:
    """Normalized formula number text, stripped of full-width characters and wrapping brackets, for use with \\tag{}."""
    tag_content = full_to_half(str(tag_content or "").strip())
    if tag_content.startswith(("(", "﹙")):
        tag_content = tag_content[1:].strip()
    if tag_content.endswith((")", "﹚")):
        tag_content = tag_content[:-1].strip()
    return tag_content


def normalize_formula_content_for_tag(formula_content: str) -> str:
    """Normalize the text of the formula to be merged and remove the display formula separators that may be carried by the model."""
    return clean_isolated_formula(str(formula_content or ""))


def build_tagged_formula_content(formula_content: str, tag_content: str) -> str | None:
    """Combine the formula body and number text into pure formula content with LaTeX and tag."""
    formula_content = normalize_formula_content_for_tag(formula_content)
    tag_content = normalize_formula_tag_content(tag_content)
    if not formula_content or not tag_content:
        return None
    return f"{formula_content}\\tag{{{tag_content}}}"
