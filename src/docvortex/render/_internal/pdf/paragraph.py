"""Partially fix CJK object line breaks and reuse measurements on normal paragraphs, maintaining ReportLab's splitting and drawing contracts."""

from __future__ import annotations

from collections.abc import Callable
from copy import copy, deepcopy
from types import FunctionType
from unicodedata import category

from reportlab.platypus import Paragraph
from reportlab.platypus import paragraph as rl_paragraph
from reportlab.platypus.paraparser import ParaFrag

from .diagnostics import report_pdf_diagnostic
from .formula import InlineFormulaImage


def _natural_cjk_unit(fragment: ParaFrag, text: str) -> rl_paragraph.cjkU:
    """Paginated formulas restore geometry from unscaled callbacks without modifying the original fragment or shared formulas."""
    definition = getattr(fragment, "cbDefn", None)
    original = getattr(definition, "_pdf_cjk_original", None)
    if original is not None:
        fragment = fragment.clone(cbDefn=original)
    return rl_paragraph.cjkU(text, fragment, "utf8")


def _fit_cjk_formula(unit: rl_paragraph.cjkU, width: float) -> rl_paragraph.cjkU:
    """Only reduce the own formula when the empty row cannot be accommodated, and retain the original callback for subsequent wide and narrow trial arrangement and paging reuse."""
    definition = getattr(unit.frag, "cbDefn", None)
    if not isinstance(getattr(definition, "image", None), InlineFormulaImage) or not 0 < width < unit.width:
        return unit
    scale = width / unit.width
    fitted = copy(definition)
    fitted._pdf_cjk_original = definition
    fitted.width = width
    fitted.height = definition.height * scale
    fitted.valign = definition.valign * scale
    return rl_paragraph.cjkU(str(unit), unit.frag.clone(cbDefn=fitted), "utf8")


def _safe_cjk_split(frags: list[ParaFrag], widths: list[float], calc_bounds: bool) -> rl_paragraph.ParaLines:
    """Inherit the line breaking rules of upstream CJK, but treat empty text callbacks as complete objects, safely shifting lines and retaining zero-width markers."""
    # Adapted from ReportLab 4.4.4/5.0.1 cjkFragSplit (BSD); see licenses/REPORTLAB.txt.
    units = []
    for fragment in frags:
        text = fragment.text
        if isinstance(text, bytes):
            text = text.decode("utf8")
        units.extend(_natural_cjk_unit(fragment, character) for character in text or [""])
    lines = []
    index = start = 0
    used = 0.0
    width = widths[0]
    while index < len(units):
        unit = units[index]
        if used == 0:
            unit = units[index] = _fit_cjk_formula(unit, width)
        index += 1
        advance = unit.width
        if hasattr(advance, "normalizedValue"):
            advance = advance.normalizedValue(width)
        used += advance
        forced = hasattr(unit.frag, "lineBreak")
        if (used > width + rl_paragraph._FUZZ and used > 0) or forced:
            extra = width - used
            if not forced:
                if unit and ord(unit) < 0x3000:
                    limit = (start + index) >> 1
                    for candidate in range(index - 1, limit, -1):
                        previous = units[candidate]
                        if previous and (category(previous) == "Zs" or ord(previous) >= 0x3000):
                            following = candidate + 1
                            if following < index:
                                next_index = following + 1
                                extra += sum(item.width for item in units[next_index:index])
                                advance = units[following].width
                                unit = units[following]
                                index = next_index
                                break
                # Empty strings are substrings of any string and must be explicitly excluded and cannot be used as hanging taboo punctuation.
                if (not unit or unit not in rl_paragraph.ALL_CANNOT_START) and index > start + 1:
                    index -= 1
                    extra += advance
            lines.append(rl_paragraph.makeCJKParaLine(units[start:index], width, width - extra, extra, forced, calc_bounds))
            width = widths[min(len(lines), len(widths) - 1)]
            start = index
            used = 0.0
    # Zero-width anchor still has drawing side effects, and the cumulative width cannot be used to decide whether to discard the tail fragment.
    if start < len(units):
        lines.append(rl_paragraph.makeCJKParaLine(units[start:], width, used, width - used, False, calc_bounds))
    return rl_paragraph.ParaLines(kind=1, lines=lines)


def _split_cjk_lines(lines: rl_paragraph.ParaLines, start: int, stop: int) -> list[ParaFrag]:
    """Directly copy the line-branched CJK fragment to avoid upstream filling in spaces for formulas or modifying the drawing state of the original paragraph."""
    return [fragment.clone() for line in lines.lines[start:stop] for fragment in line.words]


class CJKParagraph(Paragraph):
    """Provides local safe line breaking for native CJK special segments, while the rest of the content continues to adhere to the native Paragraph contract."""

    def breakLinesCJK(self, maxWidths: float | list[float] | tuple[float, ...]) -> rl_paragraph.ParaLines:
        """Only the fragments containing empty text are taken over; the existing layout of first line indentation, bullet points, and pagination headers is retained."""
        self._pdf_safe_cjk = False
        if not any(getattr(fragment, "text", None) == "" for fragment in self.frags):
            return super().breakLinesCJK(maxWidths)
        self._pdf_safe_cjk = True
        if hasattr(self, "blPara") and getattr(self, "_splitpara", False):
            return self.blPara
        widths = list(maxWidths) if isinstance(maxWidths, (list, tuple)) else [maxWidths]
        self.height = 0
        rl_paragraph._handleBulletWidth(self.bulletText, self.style, widths)
        auto_leading = getattr(self, "autoLeading", getattr(self.style, "autoLeading", ""))
        return _safe_cjk_split(self.frags, widths, auto_leading not in ("", "off"))

    def _get_split_blParaFunc(self) -> Callable[[rl_paragraph.ParaLines, int, int], list[ParaFrag]]:
        """Only the rich fragments of security group lines are split without padding spaces, and ordinary text still uses the native splitting entry."""
        if getattr(self, "_pdf_safe_cjk", False):
            return _split_cjk_lines
        return super()._get_split_blParaFunc()

    def draw(self) -> None:
        """Diagnostics are only reported for the ultra-small formulas that are finally drawn to avoid outdated alarms from trial troubleshooting."""
        if getattr(self, "_pdf_safe_cjk", False):
            for line in self.blPara.lines:
                for fragment in line.words:
                    definition = getattr(fragment, "cbDefn", None)
                    original = getattr(definition, "_pdf_cjk_original", None)
                    if original is not None and fragment.fontSize * definition.width / original.width < 6:
                        report_pdf_diagnostic(
                            "pdf_layout_small_text",
                            f"Inline formula fitted below 6 pt ({getattr(fragment, '_pdf_location', '')})",
                            getattr(fragment, "_pdf_page_idx", None),
                        )
        super().draw()


_STYLE_FIELDS = ("fontName", "fontSize", "textColor", "rise", "us_lines", "link", "backColor", "nobr")
_MISSING = object()


def _same_plain_style(first, second) -> bool:
    """Ordinary fragments are compared by identity or style dictionary to avoid triggering dynamic attribute lookups and missing attribute exceptions on a character-by-character basis."""
    if first is second:
        return True
    left, right = first.__dict__, second.__dict__
    # Keep the difference between "attribute does not exist" and "attribute value is None" in native comparison.
    return [left.get(name, _MISSING) for name in _STYLE_FIELDS] == [right.get(name, _MISSING) for name in _STYLE_FIELDS]


def _plain_cjk_breaker():
    """Only bind local comparison functions to own paragraphs, do not copy the line breaking algorithm, and do not modify the global ReportLab module."""
    replacement = _same_plain_style
    for function, dependency in (
        (getattr(rl_paragraph, "makeCJKParaLine", None), "sameFrag"),
        (getattr(rl_paragraph, "cjkFragSplit", None), "makeCJKParaLine"),
        (Paragraph.breakLinesCJK, "cjkFragSplit"),
    ):
        # ReportLab keeps returning to its original entry when its internal implementation changes or has been packaged externally.
        if not isinstance(function, FunctionType) or dependency not in function.__code__.co_names:
            return None
        namespace = function.__globals__.copy()
        namespace[dependency] = replacement
        replacement = FunctionType(function.__code__, namespace, function.__name__, function.__defaults__, function.__closure__)
        replacement.__kwdefaults__ = function.__kwdefaults__
    return replacement


_PLAIN_CJK_BREAK = _plain_cjk_breaker()


class PlainCJKParagraph(CJKParagraph):
    """Only ordinary CJK group lines use local comparison, and complex content and split paragraphs are handed over to the safe line break layer."""

    def breakLinesCJK(self, maxWidths):
        """The current fragment is checked every time, and the old qualification or style comparison cache is not used after the content changes."""
        self._pdf_safe_cjk = False
        if (
            _PLAIN_CJK_BREAK is not None
            and len(self.frags) > 1
            and not self.bulletText
            and not self.style.endDots
            and not getattr(self, "_splitpara", False)
            and all(
                type(fragment) is ParaFrag
                and type(getattr(fragment, "text", None)) is str
                and bool(fragment.text)
                and "cbDefn" not in fragment.__dict__
                and "lineBreak" not in fragment.__dict__
                and not any(fragment.__dict__.get(name) for name in ("link", "us_lines", "rise", "nobr"))
                for fragment in self.frags
            )
        ):
            return _PLAIN_CJK_BREAK(self, maxWidths)
        return super().breakLinesCJK(maxWidths)


class MeasuredParagraph(CJKParagraph):
    """The cache remains in the full layout state of this object and does not share blPara or fragments across paragraphs."""

    def _measurement_key(self, width: float) -> tuple | None:
        """Check content, style, and saved geometry; complex callbacks and conservative remeasurement of processed word lists."""
        if any(not hasattr(fragment, "__dict__") or hasattr(fragment, "cbDefn") for fragment in self.frags):
            return None
        style = vars(self.style).copy()
        # ReportLab has copied the inherited value to the current style, parent itself does not participate in wrap, and its deep copy cannot be compared by object identity.
        style.pop("parent", None)
        return (
            width,
            style,
            getattr(self, "autoLeading", None),
            tuple(vars(fragment) for fragment in self.frags),
            id(getattr(self, "blPara", None)),
            getattr(self, "width", None),
            getattr(self, "height", None),
            tuple(getattr(self, "_wrapWidths", ())),
        )

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:
        """Reuse existing line breaks when the width is the same and the status does not change; Paragraph itself does not use the available height to determine line breaks."""
        key = self._measurement_key(availWidth)
        cached = getattr(self, "_measurement_cache", None)
        if key is not None and cached is not None and key == cached[0]:
            return cached[1]
        self._measurement_cache = None
        result = super().wrap(availWidth, availHeight)
        key = self._measurement_key(availWidth)
        if key is not None:
            # Save independent snapshots only after true line breaks; use dictionary comparison on hits to avoid sequential serialization of styles and long text.
            self._measurement_cache = (deepcopy(key), result)
        return result

    def split(self, availWidth: float, availHeight: float) -> list:
        """Splitting may modify the line layout, the new paragraphs returned are cached independently, and the original objects are subsequently remeasured."""
        self._measurement_cache = None
        try:
            return super().split(availWidth, availHeight)
        finally:
            self._measurement_cache = None

    def draw(self) -> None:
        """Drawing may update the inline state, and the old results will no longer be treated as unchanged trial layout state after drawing."""
        try:
            super().draw()
        finally:
            self._measurement_cache = None


class MeasuredCJKParagraph(MeasuredParagraph, PlainCJKParagraph):
    """The combined ordinary CJK comparison is multiplexed with the original measurement, and the split paragraphs continue to have independent typesetting status."""
