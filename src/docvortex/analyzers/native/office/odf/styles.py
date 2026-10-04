"""Resolving OpenDocument style inheritance, list and logical pagination properties."""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger
from lxml import etree  # type: ignore[reportMissingImports]

from .constants import qname
from .models import ListLevel, TextStyle, TextStyleDelta


@dataclass(frozen=True, slots=True)
class _StyleDefinition:
    """Saves the parent, text delta, and cross-format projection properties of a named style."""

    name: str
    family: str
    parent: str | None
    display_name: str | None
    text_delta: TextStyleDelta
    master_page_name: str | None
    table_display: bool | None
    drawing_page_visible: bool | None


def _numeric_font_weight(value: str) -> int | None:
    """Verify lexical length and valid range of CSS numeric weight before integer conversion."""
    normalized = value.strip()
    if not normalized or len(normalized) > 4 or not normalized.isascii() or not normalized.isdigit():
        return None
    weight = int(normalized)
    return weight if 1 <= weight <= 1_000 else None


class OdfStyles:
    """Merges ODF style definitions from styles.xml and content.xml."""

    def __init__(self, *roots: etree._Element | None) -> None:
        """Collects document styles in the order they are passed in, allowing content.xml automatic styles to override the underlying definition."""
        self._styles: dict[tuple[str, str], _StyleDefinition] = {}
        self._defaults: dict[str, TextStyleDelta] = {}
        self._list_styles: dict[str, dict[int, ListLevel]] = {}
        self._resolved_text: dict[tuple[str, str], TextStyleDelta] = {}
        self._resolved_table_display: dict[str, bool | None] = {}
        self._resolved_drawing_page_visibility: dict[str, bool | None] = {}
        self._master_pages: dict[str, etree._Element] = {}
        for root in roots:
            if root is not None:
                self._collect(root)

    def _collect(self, root: etree._Element) -> None:
        """Collects default, named, list and master-page styles from a ODF XML tree."""
        for default in root.iter(qname("style", "default-style")):
            family = default.get(qname("style", "family"))
            if family:
                self._defaults[family] = self._text_delta(default)
        for style in root.iter(qname("style", "style")):
            name = style.get(qname("style", "name"))
            family = style.get(qname("style", "family"))
            if not name or not family:
                continue
            self._styles[(family, name)] = _StyleDefinition(
                name=name,
                family=family,
                parent=style.get(qname("style", "parent-style-name")),
                display_name=style.get(qname("style", "display-name")),
                text_delta=self._text_delta(style),
                master_page_name=style.get(qname("style", "master-page-name")),
                table_display=self._table_display(style),
                drawing_page_visible=self._drawing_page_visibility(style),
            )
        for list_style in root.iter(qname("text", "list-style")):
            name = list_style.get(qname("style", "name"))
            if name:
                self._list_styles[name] = self._parse_list_style(list_style)
        for master_page in root.iter(qname("style", "master-page")):
            name = master_page.get(qname("style", "name"))
            if name:
                self._master_pages[name] = master_page

    @staticmethod
    def _text_delta(style: etree._Element) -> TextStyleDelta:
        """Convert style:text-properties to an inheritable tri-state style increment."""
        properties = style.find(qname("style", "text-properties"))
        if properties is None:
            return TextStyleDelta()
        weight = properties.get(qname("fo", "font-weight"))
        bold: bool | None = None
        if weight is not None:
            normalized_weight = weight.strip().casefold()
            numeric_weight = _numeric_font_weight(normalized_weight)
            bold = numeric_weight >= 600 if numeric_weight is not None else normalized_weight == "bold"
        font_style = properties.get(qname("fo", "font-style"))
        italic = None if font_style is None else font_style.casefold() in {"italic", "oblique"}
        underline_style = properties.get(qname("style", "text-underline-style"))
        underline = None if underline_style is None else underline_style.casefold() != "none"
        strike_style = properties.get(qname("style", "text-line-through-style"))
        strikethrough = None if strike_style is None else strike_style.casefold() != "none"
        position = (properties.get(qname("style", "text-position")) or "").strip().casefold()
        superscript: bool | None = None
        subscript: bool | None = None
        if position:
            superscript = position.startswith("super") or OdfStyles._position_is_positive(position)
            subscript = position.startswith("sub") or OdfStyles._position_is_negative(position)
            if position == "0%":
                superscript = False
                subscript = False
        return TextStyleDelta(
            bold=bold,
            italic=italic,
            underline=underline,
            strikethrough=strikethrough,
            superscript=superscript,
            subscript=subscript,
        )

    @staticmethod
    def _position_is_positive(position: str) -> bool:
        """Determine whether text-position in percentage form represents superscript."""
        try:
            return float(position.split("%", 1)[0]) > 0
        except ValueError:
            return False

    @staticmethod
    def _position_is_negative(position: str) -> bool:
        """Determine whether text-position in percentage form represents a subscript."""
        try:
            return float(position.split("%", 1)[0]) < 0
        except ValueError:
            return False

    @staticmethod
    def _table_display(style: etree._Element) -> bool | None:
        """Read table style display switch for filtering hidden worksheets."""
        properties = style.find(qname("style", "table-properties"))
        if properties is None:
            return None
        display = properties.get(qname("table", "display"))
        if display is None:
            return None
        return display.casefold() != "false"

    @staticmethod
    def _drawing_page_visibility(style: etree._Element) -> bool | None:
        """Read presentation in drawing-page style visibility."""
        properties = style.find(qname("style", "drawing-page-properties"))
        if properties is None:
            return None
        visibility = properties.get(qname("presentation", "visibility"))
        if visibility is None:
            return None
        return visibility.casefold() != "hidden"

    @staticmethod
    def _parse_list_style(element: etree._Element) -> dict[int, ListLevel]:
        """Resolves the level, type, and common starting value of a list style."""
        levels: dict[int, ListLevel] = {}
        for child in element:
            if not isinstance(child.tag, str):
                continue
            local_name = etree.QName(child).localname
            if local_name not in {"list-level-style-number", "list-level-style-bullet", "list-level-style-image"}:
                continue
            try:
                level = max(1, int(child.get(qname("text", "level"), "1")))
            except ValueError:
                level = 1
            try:
                start = max(1, int(child.get(qname("text", "start-value"), "1")))
            except ValueError:
                start = 1
            ordered = local_name == "list-level-style-number" and bool(child.get(qname("style", "num-format"), "1"))
            levels[level - 1] = ListLevel(
                ordered=ordered,
                start=start,
            )
        return levels

    def _resolved_delta(self, family: str, name: str | None) -> TextStyleDelta:
        """Merge styles along parent-style-name with safe truncation at loops."""
        if not name:
            return self._defaults.get(family, TextStyleDelta())
        key = (family, name)
        if key in self._resolved_text:
            return self._resolved_text[key]
        chain: list[_StyleDefinition] = []
        seen: set[str] = set()
        current = name
        while current:
            if current in seen:
                logger.warning("ODF style inheritance cycle detected: family={}, style={}", family, current)
                break
            seen.add(current)
            definition = self._styles.get((family, current))
            if definition is None:
                break
            chain.append(definition)
            current = definition.parent or ""
        result = self._defaults.get(family, TextStyleDelta())
        for definition in reversed(chain):
            result = result.merge(definition.text_delta)
        self._resolved_text[key] = result
        return result

    def text_style(self, style_name: str | None, *, family: str, inherited: TextStyle | None = None) -> TextStyle:
        """Resolves the specified style and causes the undeclared properties of span to inherit the current paragraph style."""
        delta = self._resolved_delta(family, style_name)
        if inherited is not None:
            base = TextStyleDelta(
                bold=inherited.bold,
                italic=inherited.italic,
                underline=inherited.underline,
                strikethrough=inherited.strikethrough,
                superscript=inherited.superscript,
                subscript=inherited.subscript,
            )
            delta = base.merge(delta)
        return delta.resolve()

    def paragraph_master_page_name(self, style_name: str | None) -> str | None:
        """Finds the first valid master-page name along the paragraph parent style."""
        if not style_name:
            return None
        seen: set[str] = set()
        current = style_name
        while current:
            if current in seen:
                logger.warning("ODF style inheritance cycle detected: family=paragraph, style={}", current)
                break
            seen.add(current)
            definition = self._styles.get(("paragraph", current))
            if definition is None:
                break
            if definition.master_page_name is not None:
                return definition.master_page_name
            current = definition.parent or ""
        return None

    def is_document_title(self, style_name: str | None) -> bool:
        """Along the paragraph parent style, determine whether to inherit the document title semantics based on the name and display-name."""
        if not style_name:
            return False
        seen: set[str] = set()
        current = style_name
        while current:
            if current in seen:
                logger.warning("ODF style inheritance cycle detected: family=paragraph, style={}", current)
                break
            seen.add(current)
            definition = self._styles.get(("paragraph", current))
            names = {current, definition.display_name if definition is not None else ""}
            normalized = {name.replace("_20_", " ").replace("_", " ").strip().casefold() for name in names if name}
            if normalized & {"title", "document title", "标题"}:
                return True
            if definition is None:
                break
            current = definition.parent or ""
        return False

    def list_level(self, style_name: str | None, depth: int) -> ListLevel | None:
        """Returns the definition for the specified list depth; returns None (no visible markers) if the style is defined but has no levels."""
        if not style_name:
            return ListLevel()
        levels = self._list_styles.get(style_name)
        if levels is None:
            return ListLevel()
        if not levels:
            return None
        return levels.get(depth, ListLevel())

    def table_is_visible(self, style_name: str | None) -> bool:
        """display is parsed along the table parent style, with child style explicit values taking precedence."""
        if not style_name:
            return True
        if style_name in self._resolved_table_display:
            return self._resolved_table_display[style_name] is not False
        seen: set[str] = set()
        current = style_name
        resolved: bool | None = None
        while current:
            if current in seen:
                logger.warning("ODF style inheritance cycle detected: family=table, style={}", current)
                break
            seen.add(current)
            definition = self._styles.get(("table", current))
            if definition is None:
                break
            if definition.table_display is not None:
                resolved = definition.table_display
                break
            current = definition.parent or ""
        self._resolved_table_display[style_name] = resolved
        return resolved is not False

    def drawing_page_is_visible(self, page: etree._Element) -> bool:
        """Resolve hidden state in ODP page direct property or drawing-page style."""
        direct_visibility = page.get(qname("presentation", "visibility"))
        if direct_visibility is not None:
            return direct_visibility.casefold() != "hidden"
        style_name = page.get(qname("draw", "style-name"))
        if not style_name:
            return True
        if style_name in self._resolved_drawing_page_visibility:
            return self._resolved_drawing_page_visibility[style_name] is not False
        seen: set[str] = set()
        current = style_name
        resolved: bool | None = None
        while current:
            if current in seen:
                logger.warning("ODF style inheritance cycle detected: family=drawing-page, style={}", current)
                break
            seen.add(current)
            definition = self._styles.get(("drawing-page", current))
            if definition is None:
                break
            if definition.drawing_page_visible is not None:
                resolved = definition.drawing_page_visible
                break
            current = definition.parent or ""
        self._resolved_drawing_page_visibility[style_name] = resolved
        return resolved is not False

    def master_page(self, name: str | None) -> etree._Element | None:
        """Returns the specified master-page; if the name is empty, the first definition will be used first."""
        if name and name in self._master_pages:
            return self._master_pages[name]
        return next(iter(self._master_pages.values()), None)


__all__ = ["OdfStyles"]
