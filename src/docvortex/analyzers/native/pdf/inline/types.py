"""Defines native PDF inline style evidence, interval and internal projection types."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from .....schema import RAW_ALGORITHM, RAW_CAPTION, RAW_FOOTNOTE, RAW_PHONETIC, BBox, BlockType

TEXT_DECORATION_MIN_LENGTH_HEIGHT_RATIO = 1.8

STRIKETHROUGH_CENTER_TOLERANCE_HEIGHT_RATIO = 0.2

UNDERLINE_BOTTOM_TOLERANCE_HEIGHT_RATIO = 0.2

TEXT_DECORATION_MAX_WIDTH_HEIGHT_RATIO = 0.2

TEXT_DECORATION_MIN_TEXT_COVERAGE_RATIO = 0.55

TEXT_DECORATION_ENDPOINT_TOLERANCE_HEIGHT_RATIO = 0.5

UNDERLINE_FRACTION_MAX_GAP_HEIGHT_RATIO = 0.15

UNDERLINE_FRACTION_MIN_LOWER_LINE_COVERAGE = 0.8

PDF_FONT_ITALIC_FLAG = 1 << 6

PDF_FONT_FORCE_BOLD_FLAG = 1 << 18

PDF_BOLD_MIN_WEIGHT = 600.0

PDF_BOLD_MIN_COMPARABLE_CHAR_COUNT = 2

PDF_LINK_CHAR_OVERLAP_THRESHOLD = 0.5

PDFTextStyle = Literal[
    "bold",
    "italic",
    "underline",
    "strikethrough",
    "superscript",
    "subscript",
]

PDFScriptStyle = Literal["superscript", "subscript"]

_PDF_LINK_INTERVALS_KEY = "_inline_link_intervals"

_PDF_STYLE_INTERVALS_KEY = "_inline_style_intervals"

PDF_NATIVE_SCRIPT_MARKUP_KEY = "_pdf_native_script_markup"

_NATIVE_SCRIPT_TAG_RE = re.compile(r"<(?P<closing>/)?(?P<tag>sup|sub)>")

_PDF_INLINE_SPAN_BLOCK_TYPES = {
    BlockType.TEXT,
    BlockType.REF_TEXT,
    BlockType.DOC_TITLE,
    BlockType.PARAGRAPH_TITLE,
    BlockType.HEADER,
    BlockType.FOOTER,
    BlockType.PAGE_NUMBER,
    BlockType.ASIDE_TEXT,
    BlockType.PAGE_FOOTNOTE,
    BlockType.LIST,
    BlockType.INDEX,
    RAW_ALGORITHM,
    RAW_CAPTION,
    RAW_FOOTNOTE,
    RAW_PHONETIC,
}

PDFTextDecoration = Literal["underline", "strikethrough"]

PDF_TEXT_STYLE_ORDER: tuple[PDFTextStyle, ...] = (
    "bold",
    "italic",
    "underline",
    "strikethrough",
    "superscript",
    "subscript",
)

_PDF_TEXT_DECORATION_ORDER: tuple[PDFTextDecoration, ...] = (
    "underline",
    "strikethrough",
)

PDF_NATURAL_TEXT_STYLE_BLOCK_TYPES = frozenset(
    {
        BlockType.TEXT,
        BlockType.REF_TEXT,
        BlockType.DOC_TITLE,
        BlockType.PARAGRAPH_TITLE,
        BlockType.LIST,
        BlockType.INDEX,
        BlockType.HEADER,
        BlockType.FOOTER,
        BlockType.PAGE_NUMBER,
        BlockType.ASIDE_TEXT,
        BlockType.PAGE_FOOTNOTE,
        RAW_CAPTION,
        RAW_FOOTNOTE,
    }
)

_PDF_TEXT_STYLE_TARGET_BLOCK_TYPES: dict[PDFTextStyle, frozenset[str]] = {
    "bold": frozenset({BlockType.TEXT, BlockType.INDEX}),
    "italic": frozenset(),
    "underline": frozenset({BlockType.TEXT}),
    "strikethrough": PDF_NATURAL_TEXT_STYLE_BLOCK_TYPES,
    "superscript": PDF_NATURAL_TEXT_STYLE_BLOCK_TYPES,
    "subscript": PDF_NATURAL_TEXT_STYLE_BLOCK_TYPES,
}

_PDF_FONT_SUBSET_PREFIX_RE = re.compile(r"^[A-Z]{6}\+")

_PDF_BOLD_FONT_NAME_RE = re.compile(
    r"bold|demi|black|heavy|(?:^|[-_,])(?:bd|bi)(?:mt)?$|gbi$|(?:^|[-_,])w[6-9]$",
    re.IGNORECASE,
)

_PDF_LIST_MARKER_CHARS = frozenset("•◦‣⁃▪▫●○■□∙·▶▷►▸▹\uf0b7")

_PDF_GEOMETRIC_TEXT_STYLES = frozenset({"underline", "strikethrough"})

_PDF_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

_PDF_SEPARATOR_SPACE_CHARS = frozenset(
    "\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000"
)

_PDF_ZERO_WIDTH_CHARS = frozenset({"\u200b", "\u2060", "\ufeff"})

_LIGATURE_REPLACEMENTS = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "ﬅ": "ft",
    "ﬆ": "st",
}

_PDF_SCRIPT_TOKEN_CONNECTORS = frozenset({"^", "_", "ˆ", "~"})

_PDF_SCRIPT_COMPACT_JOINERS = frozenset({"-", "−", "–"})

_PDF_SCRIPT_CITATION_BRACKETS = {
    "[": "]",
    "［": "］",
}

_PDF_SCRIPT_AUTHOR_MARKS = frozenset({"*", "†", "‡", "∗"})

_PDF_SCRIPT_SIGN_CHARS = frozenset({"+", "-", "−", "–", "⁻", "⁺"})

_PDF_SCRIPT_SPACED_OPERATORS = frozenset({"+", "-", "−", "–"})

_PDF_SCRIPT_TRAILING_MARKS = frozenset({")", "）"})

_PDF_SCRIPT_MATH_BASE_CHARS = frozenset({"∆", "Δ", "σ", "Σ", "φ", "Φ", "μ", "µ", "Ω", "Ω", "λ", "γ", "δ", "ρ", "β", "α"})


@dataclass(frozen=True, slots=True)
class PDFTextStyleRange:
    """Saves a half-open style range in comparable text."""

    start: int
    end: int
    styles: tuple[PDFTextStyle, ...]


@dataclass(frozen=True, slots=True)
class PDFTextStyleLine:
    """Saves the geometry, comparable text, and style intervals of a visual text run."""

    bbox: BBox
    text: str
    style_ranges: tuple[PDFTextStyleRange, ...]
    source_index: int


@dataclass(frozen=True, slots=True)
class PDFTextScriptRange:
    """Save Flash superscript and subscript text ranges, pages tight bbox and stability evidence."""

    start: int
    end: int
    style: PDFScriptStyle
    bbox: BBox
    stable_body_count: int
    formula_region: bool


@dataclass(frozen=True, slots=True)
class PDFTextScriptLine:
    """Saves compact text and superscript and subscript candidates for a Flash visual line."""

    bbox: BBox
    text: str
    script_ranges: tuple[PDFTextScriptRange, ...]
    source_index: int
    angle: int


@dataclass(frozen=True, slots=True)
class PDFTextLinkRange:
    """Saves a half-open hyperlink range in comparable text."""

    start: int
    end: int
    target: str


@dataclass(frozen=True, slots=True)
class PDFTextLinkLine:
    """Save geometry, comparable text, and hyperlink intervals for visual text run."""

    bbox: BBox
    text: str
    link_ranges: tuple[PDFTextLinkRange, ...]
    source_index: int


PDFTextEvidenceLine = TypeVar(
    "PDFTextEvidenceLine",
    PDFTextStyleLine,
    PDFTextLinkLine,
    PDFTextScriptLine,
)


@dataclass(frozen=True, slots=True)
class _VisibleChar:
    """Save the visible characters that participate in the geometric judgment of text decoration lines."""

    source_index: int
    bbox: BBox


@dataclass(slots=True)
class _LineCandidate:
    """Saves the visual text line metrics used in the font and text decoration line matching phase."""

    bbox: BBox
    chars: list[dict[str, Any]]
    visible_chars: list[_VisibleChar]
    median_height: float
    center_y: float
    bottom_y: float
    source_index: int
    font_styles: list[frozenset[PDFTextStyle]]
    decoration_ranges: dict[PDFTextDecoration, list[tuple[int, int]]]


@dataclass(frozen=True, slots=True)
class _DrawingMatch:
    """Save the matching results and sorting indicators of a single drawing for a single text line."""

    style: PDFTextDecoration
    start_index: int
    end_index: int
    target_distance_ratio: float
    horizontal_overlap_ratio: float


@dataclass(frozen=True, slots=True)
class _ProjectedChar:
    """Save model content Mapping of comparable characters to original string positions."""

    value: str
    raw_start: int
    raw_end: int
    existing_styles: frozenset[PDFTextStyle]
    formula_gap_before: bool
    inside_hyperlink: bool


@dataclass(frozen=True, slots=True)
class _RawStyleInterval:
    """Save model content The half-open style interval in the original string."""

    start: int
    end: int
    styles: tuple[PDFTextStyle, ...]


@dataclass(frozen=True, slots=True)
class _NativeScriptMarkup:
    """Save detector-owned superscript and subscript label boundaries and corresponding original text style intervals."""

    marker_ranges: tuple[tuple[int, int], ...]
    style_intervals: tuple[tuple[int, int, str], ...]


@dataclass(frozen=True, slots=True)
class _MatchedLinkRange:
    """Saves linked ranges with physical line identities that have been aligned to block comparable text."""

    start: int
    end: int
    target: str
    source_index: int


@dataclass(frozen=True, slots=True)
class _RawLinkInterval:
    """Save model content The half-open link interval in the original string."""

    start: int
    end: int
    target: str
    source_index: int


@dataclass(frozen=True, slots=True)
class _LineProjectionMatch:
    """Save results of physical line alignment across formula holes to block comparable text."""

    start: int
    end: int
    source_to_projected: tuple[int | None, ...]


__all__ = [
    "TEXT_DECORATION_MIN_LENGTH_HEIGHT_RATIO",
    "STRIKETHROUGH_CENTER_TOLERANCE_HEIGHT_RATIO",
    "UNDERLINE_BOTTOM_TOLERANCE_HEIGHT_RATIO",
    "TEXT_DECORATION_MAX_WIDTH_HEIGHT_RATIO",
    "TEXT_DECORATION_MIN_TEXT_COVERAGE_RATIO",
    "TEXT_DECORATION_ENDPOINT_TOLERANCE_HEIGHT_RATIO",
    "UNDERLINE_FRACTION_MAX_GAP_HEIGHT_RATIO",
    "UNDERLINE_FRACTION_MIN_LOWER_LINE_COVERAGE",
    "PDF_FONT_ITALIC_FLAG",
    "PDF_FONT_FORCE_BOLD_FLAG",
    "PDF_BOLD_MIN_WEIGHT",
    "PDF_BOLD_MIN_COMPARABLE_CHAR_COUNT",
    "PDF_LINK_CHAR_OVERLAP_THRESHOLD",
    "PDFTextStyle",
    "PDFScriptStyle",
    "_PDF_LINK_INTERVALS_KEY",
    "_PDF_STYLE_INTERVALS_KEY",
    "PDF_NATIVE_SCRIPT_MARKUP_KEY",
    "_NATIVE_SCRIPT_TAG_RE",
    "_PDF_INLINE_SPAN_BLOCK_TYPES",
    "PDFTextDecoration",
    "PDF_TEXT_STYLE_ORDER",
    "_PDF_TEXT_DECORATION_ORDER",
    "PDF_NATURAL_TEXT_STYLE_BLOCK_TYPES",
    "_PDF_TEXT_STYLE_TARGET_BLOCK_TYPES",
    "_PDF_FONT_SUBSET_PREFIX_RE",
    "_PDF_BOLD_FONT_NAME_RE",
    "_PDF_LIST_MARKER_CHARS",
    "_PDF_GEOMETRIC_TEXT_STYLES",
    "_PDF_CONTROL_CHAR_RE",
    "_PDF_SEPARATOR_SPACE_CHARS",
    "_PDF_ZERO_WIDTH_CHARS",
    "_LIGATURE_REPLACEMENTS",
    "_PDF_SCRIPT_TOKEN_CONNECTORS",
    "_PDF_SCRIPT_COMPACT_JOINERS",
    "_PDF_SCRIPT_CITATION_BRACKETS",
    "_PDF_SCRIPT_AUTHOR_MARKS",
    "_PDF_SCRIPT_SIGN_CHARS",
    "_PDF_SCRIPT_SPACED_OPERATORS",
    "_PDF_SCRIPT_TRAILING_MARKS",
    "_PDF_SCRIPT_MATH_BASE_CHARS",
    "PDFTextStyleRange",
    "PDFTextStyleLine",
    "PDFTextScriptRange",
    "PDFTextScriptLine",
    "PDFTextLinkRange",
    "PDFTextLinkLine",
    "PDFTextEvidenceLine",
    "_VisibleChar",
    "_LineCandidate",
    "_DrawingMatch",
    "_ProjectedChar",
    "_RawStyleInterval",
    "_NativeScriptMarkup",
    "_MatchedLinkRange",
    "_RawLinkInterval",
    "_LineProjectionMatch",
]
