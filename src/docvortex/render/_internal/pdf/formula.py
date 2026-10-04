"""ZiaMath LaTeX to controlled ReportLab vector path conversion."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from threading import RLock
from typing import Any
from xml.etree import ElementTree

from fontTools.pens.basePen import BasePen
from fontTools.svgLib.path import parse_path
from reportlab.graphics.shapes import Drawing, Group, Path, Rect
from reportlab.lib.colors import Color, toColor
from reportlab.platypus import Flowable
import ziamath
from ziamath.tex import tex2mml

from .diagnostics import report_pdf_diagnostic

MAX_FORMULA_CHARACTERS = 20_000
MAX_CACHED_FORMULAS = 512
_MAX_SVG_NODES = 20_000
_MAX_SVG_PATH_CHARACTERS = 20_000_000
_SVG_NAMESPACE = "http://www.w3.org/2000/svg"
_ZIAMATH_LOCK = RLock()
_SVG_PATH_COMMAND_RE = re.compile(r"[A-DF-Za-df-z]")


class PdfFormulaError(ValueError):
    """Indicates that LaTeX or ZiaMath SVG cannot be safely converted to a PDF vector object."""


@dataclass(frozen=True, slots=True)
class FormulaVector:
    """Save a formula for ReportLab Drawing with baseline geometry."""

    drawing: Drawing
    width: float
    height: float
    ascent: float
    descent: float
    axis_height: float = 0.0
    multiline: bool = False

    def scaled(self, factor: float) -> FormulaVector:
        """Returns a proportional formula object that only adjusts the display geometry and does not copy Drawing."""
        return FormulaVector(
            drawing=self.drawing,
            width=self.width * factor,
            height=self.height * factor,
            ascent=self.ascent * factor,
            descent=self.descent * factor,
            axis_height=self.axis_height * factor,
            multiline=self.multiline,
        )


@dataclass(frozen=True, slots=True)
class InlineFormulaImage:
    """Acts as a vector formula proxy for the ReportLab Paragraph inline picture placeholder."""

    vector: FormulaVector


class FormulaRenderer:
    """Maintaining a formula cache that is bounded within a single PDF document and has no cross-document state."""

    def __init__(self) -> None:
        """Initializes a document-level cache that caches up to 512 unique formulas."""
        self._cache: dict[tuple[str, bool, float, str], FormulaVector] = {}

    def render(
        self,
        latex: str,
        *,
        inline: bool,
        font_size: float,
        color: str = "#1f2937",
    ) -> FormulaVector:
        """Converts a bare LaTeX formula to an inline or interline vector object."""
        if not isinstance(latex, str) or not latex.strip():
            raise PdfFormulaError("formula must contain non-blank LaTeX")
        if not isinstance(font_size, (int, float)) or isinstance(font_size, bool) or font_size <= 0:
            raise PdfFormulaError("font_size must be a positive number")
        if len(latex) > MAX_FORMULA_CHARACTERS:
            raise PdfFormulaError(f"formula exceeds max_formula_characters={MAX_FORMULA_CHARACTERS}")

        key = (latex, inline, float(font_size), color)
        if key in self._cache:
            return self._cache[key]

        vector = _render_ziamath_formula(latex, inline=inline, font_size=float(font_size), color=color)
        if len(self._cache) < MAX_CACHED_FORMULAS:
            self._cache[key] = vector
        return vector


class DisplayFormulaFlowable(Flowable):
    """Draw the formula centered within the available line width, with optional numbering taped to the right margin."""

    def __init__(
        self,
        formula: FormulaVector,
        tag: FormulaVector | None = None,
        *,
        font_size: float = 10.5,
        location: str = "",
        page_index: int | None = None,
    ) -> None:
        """Saves formulas, optional numbers, and scaling parameters that are deferred to the wrap stage calculation."""
        super().__init__()
        self.formula = formula
        self.tag = tag
        self.font_size = font_size
        self.location = location
        self.page_index = page_index
        self._available_width = formula.width
        self._formula_scale = 1.0
        self._tag_scale = 1.0
        self.width = formula.width
        self.height = formula.height
        self.formula_rect = (0.0, 0.0, formula.width, formula.height)
        self.tag_rect: tuple[float, float, float, float] | None = None

    @property
    def effective_font_size(self) -> float:
        """Returns the base font size of the formula body when it is finally drawn."""
        return self.font_size * self._formula_scale

    @property
    def effective_tag_font_size(self) -> float | None:
        """Returns the serial number font size after independent scaling, and returns a null value if there is no serial number."""
        return self.font_size * self._tag_scale if self.tag is not None else None

    def wrap(self, avail_width: float, _avail_height: float) -> tuple[float, float]:
        """The rearranged version is only trial layout based on line width, and the remaining height at the footer is left to the pager."""
        self.fit_to_box(avail_width, font_size=self.font_size)
        return self.width, self.height

    def fit_to_box(self, width: float, *, font_size: float, max_height: float | None = None) -> None:
        """Retry the arrangement from the original vector every time; if the height is insufficient, the body will be reduced first, and the entire column of coordinates will always be retained."""
        self.width = self._available_width = max(0.001, width)
        target_scale = font_size / self.font_size
        gap = font_size if self.tag is not None else 0.0
        tag_scale = min(target_scale, self.width / self.tag.width) if self.tag is not None else target_scale
        tag_width = self.tag.width * tag_scale if self.tag is not None else 0.0
        # When the number is very long or the column width is less than one word, it is more readable to occupy the last line than to compress the number into tiny text.
        stacked = self.tag is not None and (tag_width > self.width * 0.4 or self.width - tag_width - gap < font_size)
        main_limit = self.width if stacked else max(0.001, self.width - tag_width - gap)
        formula_scale = min(target_scale, main_limit / self.formula.width)
        self._position(formula_scale, tag_scale, gap, stacked)
        if max_height is None or self.height <= max_height:
            return
        height_limit = max(0.001, max_height)
        # First determine whether the serial number itself can retain the target font size; only when the extremely narrow and short frame reduces the serial number and line spacing year-on-year.
        self._position(0.0, tag_scale, gap, stacked)
        if self.height >= height_limit:
            ratio = height_limit / max(self.height, 0.001) * 0.5
            tag_scale *= ratio
            gap *= ratio
        low, high = 0.0, formula_scale
        for _ in range(40):
            candidate = (low + high) / 2
            self._position(candidate, tag_scale, gap, stacked)
            if self.height <= height_limit:
                low = candidate
            else:
                high = candidate
        self._position(low, tag_scale, gap, stacked)

    def _position(self, formula_scale: float, tag_scale: float, gap: float, stacked: bool) -> None:
        """The final rectangle of the ontology and serial number is calculated uniformly, using the mathematical axis for a single row and the vertical center for multiple rows."""
        self._formula_scale, self._tag_scale = formula_scale, tag_scale
        fw, fh = self.formula.width * formula_scale, self.formula.height * formula_scale
        fx, fy = max(0.0, (self.width - fw) / 2), 0.0
        self.tag_rect = None
        self.height = fh
        if self.tag is not None:
            tw, th = self.tag.width * tag_scale, self.tag.height * tag_scale
            tx = self.width - tw
            if stacked:
                fy, ty = th + gap, 0.0
            else:
                fx = max(0.0, min(fx, tx - gap - fw))
                if self.formula.multiline:
                    ty = (fh - th) / 2
                else:
                    ty = (self.formula.descent + self.formula.axis_height) * formula_scale
                    ty -= (self.tag.descent + self.tag.axis_height) * tag_scale
                fy = max(0.0, -ty)
                ty += fy
            self.tag_rect = (tx, ty, tx + tw, ty + th)
            self.height = max(fy + fh, ty + th)
        self.formula_rect = (fx, fy, fx + fw, fy + fh)

    def draw(self) -> None:
        """Drawn using the results of the last pass; undersize formulas report readability diagnostics in both layouts."""
        if min(self.effective_font_size, self.effective_tag_font_size or self.effective_font_size) < 6 - 0.001:
            report_pdf_diagnostic(
                "pdf_layout_small_text",
                f"Formula below 6 pt: formula={self.effective_font_size:.4f}, "
                f"tag={self.effective_tag_font_size}, {self.location}",
                self.page_index,
            )
        _draw_vector(self.canv, self.formula, *self.formula_rect[:2], self._formula_scale)
        if self.tag is not None and self.tag_rect is not None:
            _draw_vector(self.canv, self.tag, *self.tag_rect[:2], self._tag_scale)


class _ReportLabPathPen(BasePen):
    """Write FontTools SVG path callback to ReportLab Path."""

    def __init__(self) -> None:
        """Create an empty ReportLab path that does not depend on glyphSet."""
        super().__init__(None)
        self.path = Path()

    def _moveTo(self, point: tuple[float, float]) -> None:
        """Write the SVG move command to the target path."""
        self.path.moveTo(*point)

    def _lineTo(self, point: tuple[float, float]) -> None:
        """Write the SVG line command to the target path."""
        self.path.lineTo(*point)

    def _curveToOne(
        self,
        point1: tuple[float, float],
        point2: tuple[float, float],
        point3: tuple[float, float],
    ) -> None:
        """Write the cubic curve into the target path, and the quadratic curve is automatically converted by BasePen."""
        self.path.curveTo(*point1, *point2, *point3)

    def _closePath(self) -> None:
        """Close the current ReportLab subpath."""
        self.path.closePath()

    def _endPath(self) -> None:
        """End unclosed SVG subpath."""


def split_formula_tag(content: str) -> tuple[str, str | None]:
    """Strips the balanced ``\\tag{...}`` of the parentheses at the end of the formula and returns the text and numbering."""
    stripped_end = len(content.rstrip())
    if stripped_end == 0 or content[stripped_end - 1] != "}":
        return content, None
    search_end = stripped_end
    while (tag_start := content.rfind(r"\tag", 0, search_end)) >= 0:
        if not _is_escaped_character(content, tag_start):
            opening_brace = _find_tag_opening_brace(content, tag_start, stripped_end)
            if opening_brace is not None:
                closing_brace = _find_balanced_closing_brace(content, opening_brace, stripped_end)
                if closing_brace == stripped_end - 1:
                    return content[:tag_start].rstrip(), content[opening_brace + 1 : closing_brace].strip()
        search_end = tag_start
    return content, None


def draw_inline_formula(
    canvas: Any,
    image: InlineFormulaImage,
    x: float,
    y: float,
    width: float,
    height: float,
) -> tuple[float, float]:
    """Draws an inline vector formula by custom Canvas at the position calculated by Paragraph."""
    vector = image.vector
    scale = min(width / max(vector.width, 1.0), height / max(vector.height, 1.0))
    _draw_vector(canvas, vector, x, y, scale)
    return width, height


def _render_ziamath_formula(latex: str, *, inline: bool, font_size: float, color: str) -> FormulaVector:
    """Temporarily close SVG2 symbols within a global lock and convert a single ZiaMath result."""
    try:
        with _ZIAMATH_LOCK:
            previous_svg2 = ziamath.config.svg2
            ziamath.config.svg2 = False
            try:
                if inline:
                    formula = ziamath.Latex(latex, inline=True, size=font_size, color=color, margin=0)
                else:
                    # Conversions using ZiaMath itself preserve existing LaTeX preprocessing of aligned, operators, etc.
                    mathml = ElementTree.fromstring(tex2mml(latex, inline=False))
                    _normalize_display_mathml(mathml)
                    mathml.set("mathcolor", color)
                    formula = ziamath.Math(mathml, size=font_size, margin=0)
                root = formula.svgxml()
                axis_height = font_size * formula.font.math.consts.axisHeight / formula.font.info.layout.unitsperem
            finally:
                ziamath.config.svg2 = previous_svg2
        return replace(
            _svg_root_to_vector(root),
            axis_height=axis_height,
            multiline=bool(re.search(r"\\begin\s*\{(?:aligned|align\*?|gathered|gather\*?|split|eqnarray\*?)\}", latex)),
        )
    except PdfFormulaError:
        raise
    except Exception as exc:
        raise PdfFormulaError(f"LaTeX formula cannot be rendered: {_formula_preview(latex)!r}") from exc


def _normalize_display_mathml(element: ElementTree.Element, displaystyle: bool = True) -> None:
    """Supplement compact styles for fractional children in temporary trees, preserving explicit styles and the node structure of limits."""
    displaystyle = element.get("displaystyle", str(displaystyle).lower()) == "true"
    name = _local_name(element.tag)
    if name == "mo" and element.text == "∑" and not displaystyle:
        element.attrib.setdefault("stretchy", "false")
    for child in element:
        if name == "mfrac":
            # The explicit displaystyle attribute and its descendant mstyle override the default inheritance without changing the original LaTeX.
            child.attrib.setdefault("displaystyle", "false")
        _normalize_display_mathml(child, displaystyle)


def _svg_root_to_vector(root: ElementTree.Element) -> FormulaVector:
    """Converts a fixed SVG subset of ZiaMath to ReportLab Drawing with coordinates flipped."""
    namespace = root.get("xmlns") if root.tag == "svg" else root.tag.removeprefix("{").split("}", 1)[0]
    if _local_name(root.tag) != "svg" or namespace != _SVG_NAMESPACE:
        raise PdfFormulaError("ZiaMath output must contain an SVG root")
    view_box = _parse_number_list(root.get("viewBox"), count=4, field="viewBox")
    min_x, min_y, width, height = view_box
    if width <= 0 or height <= 0:
        raise PdfFormulaError("ZiaMath SVG dimensions must be positive")
    drawing = Drawing(width, height)
    root_group = Group()
    root_group.transform = (1, 0, 0, -1, -min_x, min_y + height)
    node_counter = [0]
    path_budget = [0]
    for child in root:
        _append_svg_element(
            root_group,
            child,
            inherited={},
            node_counter=node_counter,
            path_budget=path_budget,
        )
    drawing.add(root_group)
    ascent = max(0.0, -min_y)
    descent = max(0.0, min_y + height)
    return FormulaVector(drawing=drawing, width=width, height=height, ascent=ascent, descent=descent)


def _append_svg_element(
    parent: Group,
    element: ElementTree.Element,
    *,
    inherited: dict[str, str],
    node_counter: list[int],
    path_budget: list[int],
) -> None:
    """Recursive conversion of ZiaMath allows group, path and rect nodes."""
    node_counter[0] += 1
    if node_counter[0] > _MAX_SVG_NODES:
        raise PdfFormulaError("ZiaMath SVG exceeds its node limit")
    tag = _local_name(element.tag)
    if tag == "g":
        _reject_unknown_attributes(element, {"fill", "stroke", "stroke-width"})
        group_style = {**inherited, **element.attrib}
        group = Group()
        for child in element:
            _append_svg_element(
                group,
                child,
                inherited=group_style,
                node_counter=node_counter,
                path_budget=path_budget,
            )
        parent.add(group)
        return
    if tag == "path":
        _reject_unknown_attributes(element, {"d", "fill", "stroke", "stroke-width"})
        path_data = element.get("d", "")
        path_budget[0] += len(path_data)
        if not path_data or path_budget[0] > _MAX_SVG_PATH_CHARACTERS:
            raise PdfFormulaError("ZiaMath SVG path data is empty or exceeds its limit")
        commands = set(_SVG_PATH_COMMAND_RE.findall(path_data))
        if not commands.issubset({"M", "L", "Q", "Z"}):
            raise PdfFormulaError(f"Unsupported ZiaMath SVG path commands: {', '.join(sorted(commands))}")
        pen = _ReportLabPathPen()
        try:
            parse_path(path_data, pen)
        except Exception as exc:
            raise PdfFormulaError("ZiaMath SVG path data is invalid") from exc
        _apply_paint(pen.path, element.attrib, inherited)
        parent.add(pen.path)
        return
    if tag == "rect":
        _reject_unknown_attributes(element, {"x", "y", "width", "height", "fill", "stroke", "stroke-width"})
        x, y, width, height = (_parse_number(element.get(name, "0"), field=name) for name in ("x", "y", "width", "height"))
        if width < 0 or height < 0:
            raise PdfFormulaError("ZiaMath SVG rect dimensions must not be negative")
        rectangle = Rect(x, y, width, height)
        _apply_paint(rectangle, element.attrib, inherited)
        parent.add(rectangle)
        return
    raise PdfFormulaError(f"Unsupported ZiaMath SVG element: {tag}")


def _apply_paint(shape: Any, attributes: dict[str, str], inherited: dict[str, str]) -> None:
    """Map controlled SVG fill, stroke and stroke-width to ReportLab shape."""
    fill = attributes.get("fill", inherited.get("fill", "black"))
    stroke = attributes.get("stroke", inherited.get("stroke", "none"))
    shape.fillColor = _parse_color(fill)
    shape.strokeColor = _parse_color(stroke)
    stroke_width = attributes.get("stroke-width", inherited.get("stroke-width", "1"))
    shape.strokeWidth = _parse_number(stroke_width, field="stroke-width")


def _parse_color(value: str) -> Color | None:
    """Parse the static color generated by ZiaMath, none maps to transparent."""
    if value.strip().casefold() == "none":
        return None
    try:
        return toColor(value)
    except Exception as exc:
        raise PdfFormulaError(f"Unsupported ZiaMath SVG color: {value!r}") from exc


def _parse_number(value: str, *, field: str) -> float:
    """Strictly reads limited SVG values without CSS units."""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PdfFormulaError(f"Invalid ZiaMath SVG {field}: {value!r}") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise PdfFormulaError(f"Invalid ZiaMath SVG {field}: {value!r}")
    return number


def _parse_number_list(value: str | None, *, count: int, field: str) -> tuple[float, ...]:
    """Reads a fixed-length whitespace or comma-separated list of SVG values."""
    if value is None:
        raise PdfFormulaError(f"ZiaMath SVG is missing {field}")
    values = tuple(_parse_number(item, field=field) for item in value.replace(",", " ").split())
    if len(values) != count:
        raise PdfFormulaError(f"ZiaMath SVG {field} must contain {count} numbers")
    return values


def _reject_unknown_attributes(element: ElementTree.Element, allowed: set[str]) -> None:
    """Reject SVG attributes outside the fixed subset of ZiaMath."""
    unexpected = set(element.attrib) - allowed
    if unexpected:
        raise PdfFormulaError(f"Unsupported ZiaMath SVG attributes: {', '.join(sorted(unexpected))}")


def _local_name(tag: str) -> str:
    """Returns the local name of the element, which may take XML or namespace."""
    return tag.rsplit("}", 1)[-1]


def _formula_preview(latex: str) -> str:
    """Generate bounded LaTeX summaries for diagnostics to avoid contaminating logs with overlong formulas."""
    return latex if len(latex) <= 200 else f"{latex[:197]}..."


def _draw_vector(canvas: Any, vector: FormulaVector, x: float, y: float, scale: float) -> None:
    """Draws the formula Drawing on canvas at the given position and scale."""
    canvas.saveState()
    try:
        canvas.translate(x, y)
        canvas.scale(scale, scale)
        vector.drawing.drawOn(canvas, 0, 0)
    finally:
        canvas.restoreState()


def _find_tag_opening_brace(content: str, tag_start: int, content_end: int) -> int | None:
    """Find tag command allows white space after opening curly brace."""
    cursor = tag_start + len(r"\tag")
    while cursor < content_end and content[cursor].isspace():
        cursor += 1
    return cursor if cursor < content_end and content[cursor] == "{" else None


def _find_balanced_closing_brace(content: str, opening_brace: int, content_end: int) -> int | None:
    """Finds a closing brace paired with a tag opening brace, and ignores escaped braces."""
    depth = 0
    for cursor in range(opening_brace, content_end):
        character = content[cursor]
        if character not in "{}" or _is_escaped_character(content, cursor):
            continue
        depth += 1 if character == "{" else -1
        if depth == 0:
            return cursor
        if depth < 0:
            return None
    return None


def _is_escaped_character(content: str, position: int) -> bool:
    """Determines whether there is an odd number of consecutive backslashes before the specified character."""
    preceding_backslashes = 0
    cursor = position - 1
    while cursor >= 0 and content[cursor] == "\\":
        preceding_backslashes += 1
        cursor -= 1
    return preceding_backslashes % 2 == 1


__all__ = [
    "DisplayFormulaFlowable",
    "FormulaRenderer",
    "FormulaVector",
    "InlineFormulaImage",
    "MAX_CACHED_FORMULAS",
    "MAX_FORMULA_CHARACTERS",
    "PdfFormulaError",
    "draw_inline_formula",
    "split_formula_tag",
]
