"""Parsing XHTML/HTML uses a limited semantic subset of CSS."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree  # type: ignore[reportMissingImports]

from ...foundation.type_identity import preserve_type_module
from ...foundation.xml_names import local_name

_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_CSS_IMPORTANT_RE = re.compile(r"!\s*important\s*$", re.IGNORECASE)
_TEXT_STYLE_FIELDS = ("bold", "italic", "underline", "strikethrough", "superscript", "subscript")
_VISIBILITY_FIELDS = ("display", "visibility", "opacity")


@dataclass(frozen=True, slots=True)
class TextStyle:
    """Save text styles that can be projected to the Middle JSON inline protocol."""

    bold: bool = False
    italic: bool = False
    underline: bool = False
    strikethrough: bool = False
    superscript: bool = False
    subscript: bool = False

    def merge(self, other: TextStyle) -> TextStyle:
        """Merges inherited styles with styles explicitly enabled on the current element."""
        return TextStyle(
            bold=self.bold or other.bold,
            italic=self.italic or other.italic,
            underline=self.underline or other.underline,
            strikethrough=self.strikethrough or other.strikethrough,
            superscript=self.superscript or other.superscript,
            subscript=self.subscript or other.subscript,
        )

    def names(self) -> tuple[str, ...]:
        """Returns the style names recognized by existing inline protocols in stable order."""
        return tuple(
            name
            for enabled, name in (
                (self.bold, "bold"),
                (self.italic, "italic"),
                (self.underline, "underline"),
                (self.strikethrough, "strikethrough"),
                (self.superscript, "superscript"),
                (self.subscript, "subscript"),
            )
            if enabled
        )


@dataclass(frozen=True, slots=True)
class TextStyleDelta:
    """Save CSS The explicit on, off, or undeclared state of each text style."""

    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    strikethrough: bool | None = None
    superscript: bool | None = None
    subscript: bool | None = None

    def apply(self, base: TextStyle) -> TextStyle:
        """Overrides the current declaration to the resolved inheritance/tag style."""
        return TextStyle(
            bold=base.bold if self.bold is None else self.bold,
            italic=base.italic if self.italic is None else self.italic,
            underline=base.underline if self.underline is None else self.underline,
            strikethrough=base.strikethrough if self.strikethrough is None else self.strikethrough,
            superscript=base.superscript if self.superscript is None else self.superscript,
            subscript=base.subscript if self.subscript is None else self.subscript,
        )

    def is_empty(self) -> bool:
        """Returns whether the current declaration does not touch any supported styles."""
        return all(
            value is None
            for value in (
                self.bold,
                self.italic,
                self.underline,
                self.strikethrough,
                self.superscript,
                self.subscript,
            )
        )


@dataclass(frozen=True, slots=True)
class ElementStyle:
    """Save the element's final text style, tree-wide hidden state, and inherited visibility."""

    text: TextStyle
    subtree_hidden: bool = False
    visibility_hidden: bool = False

    @property
    def hidden(self) -> bool:
        """Returns whether the current element is invisible due to any of the supported hiding semantics."""
        return self.subtree_hidden or self.visibility_hidden


@dataclass(slots=True)
class _SelectorCascade:
    """Aggregate the last declaration of each attribute and its source code sequence by selector."""

    priority: int
    declarations: dict[str, tuple[bool, int, bool]] = field(default_factory=dict)
    visibility: dict[str, tuple[bool, int, bool]] = field(default_factory=dict)

    def update(self, parsed: _ParsedDeclarations, order: int) -> None:
        """Update the attribute-by-attribute cascade results of the same selector in order of importance and source code."""
        for name, (important, value) in parsed.text.items():
            current = self.declarations.get(name)
            if current is None or (important, order) >= current[:2]:
                self.declarations[name] = (important, order, value)
        for name, (important, value) in parsed.visibility.items():
            current = self.visibility.get(name)
            if current is None or (important, order) >= current[:2]:
                self.visibility[name] = (important, order, value)


@dataclass(frozen=True, slots=True)
class _ParsedDeclarations:
    """Saves the importance and Boolean value of the projected CSS attribute."""

    text: dict[str, tuple[bool, bool]]
    visibility: dict[str, tuple[bool, bool]]


def _numeric_font_weight(value: str) -> int | None:
    """Parse CSS before integer conversion Fonts Allowed word weight from one to one thousand."""
    if not value.isascii() or not value.isdigit() or len(value) > 4:
        return None
    weight = int(value)
    return weight if 1 <= weight <= 1_000 else None


def _parse_declarations(value: str) -> _ParsedDeclarations:
    """Extract font semantics, hidden state, and important priority from declaration string properties."""
    text: dict[str, tuple[bool, bool]] = {}
    visibility: dict[str, tuple[bool, bool]] = {}
    for raw_declaration in value.split(";"):
        if ":" not in raw_declaration:
            continue
        name, raw_value = raw_declaration.split(":", 1)
        name = name.strip().casefold()
        important_match = _CSS_IMPORTANT_RE.search(raw_value)
        important = important_match is not None
        normalized = raw_value[: important_match.start() if important_match is not None else None].strip().casefold()
        text_updates: dict[str, bool] = {}
        visibility_update: tuple[str, bool] | None = None
        if name == "font-weight":
            if normalized in {"bold", "bolder"}:
                text_updates["bold"] = True
            elif normalized in {"normal", "lighter"}:
                text_updates["bold"] = False
            elif (weight := _numeric_font_weight(normalized)) is not None:
                text_updates["bold"] = weight >= 600
        elif name == "font-style":
            text_updates["italic"] = normalized in {"italic", "oblique"}
        elif name in {"text-decoration", "text-decoration-line"}:
            if normalized == "none":
                text_updates["underline"] = False
                text_updates["strikethrough"] = False
            else:
                text_updates["underline"] = "underline" in normalized
                text_updates["strikethrough"] = "line-through" in normalized
        elif name == "vertical-align":
            text_updates["superscript"] = normalized in {"super", "text-top"}
            text_updates["subscript"] = normalized in {"sub", "text-bottom"}
        elif name == "display":
            visibility_update = ("display", normalized == "none")
        elif name == "visibility":
            if normalized in {"hidden", "collapse"}:
                visibility_update = ("visibility", True)
            elif normalized in {"visible", "initial"}:
                visibility_update = ("visibility", False)
        elif name == "opacity":
            try:
                opacity = float(normalized)
            except ValueError:
                pass
            else:
                visibility_update = ("opacity", opacity <= 0)
        for field_name, field_value in text_updates.items():
            current = text.get(field_name)
            if current is None or important or not current[0]:
                text[field_name] = (important, field_value)
        if visibility_update is not None:
            field_name, field_value = visibility_update
            current = visibility.get(field_name)
            if current is None or important or not current[0]:
                visibility[field_name] = (important, field_value)
    return _ParsedDeclarations(text=text, visibility=visibility)


class MarkupStylesheet:
    """Saves a simple tag/class CSS rule that parses in document order."""

    def __init__(self) -> None:
        """Initialize the selector index bucketed by tag, class, and tag.class."""
        self._tag_cascades: dict[str, _SelectorCascade] = {}
        self._class_cascades: dict[str, _SelectorCascade] = {}
        self._tag_class_cascades: dict[tuple[str, str], _SelectorCascade] = {}
        self._source_order = 0

    def _selector_cascade(self, tag: str | None, class_name: str | None, priority: int) -> _SelectorCascade:
        """Returns the aggregate cascade slot for the specified simple selector."""
        if class_name is None:
            assert tag is not None
            return self._tag_cascades.setdefault(tag, _SelectorCascade(priority))
        if tag is None:
            return self._class_cascades.setdefault(class_name, _SelectorCascade(priority))
        return self._tag_class_cascades.setdefault((tag, class_name), _SelectorCascade(priority))

    def add(self, css: str) -> None:
        """Appends a simple selector rule supported in stylesheet."""
        normalized_css = _CSS_COMMENT_RE.sub("", css)
        for chunk in normalized_css.split("}"):
            if "{" not in chunk:
                continue
            selectors, declarations = chunk.split("{", 1)
            parsed_declarations = _parse_declarations(declarations)
            if not parsed_declarations.text and not parsed_declarations.visibility:
                continue
            for selector in selectors.split(","):
                parsed = self._parse_selector(selector)
                if parsed is None:
                    continue
                tag, class_name, priority = parsed
                cascade = self._selector_cascade(tag, class_name, priority)
                cascade.update(parsed_declarations, self._source_order)
                self._source_order += 1

    @staticmethod
    def _parse_selector(selector: str) -> tuple[str | None, str | None, int] | None:
        """Only tag, .class and tag.class are accepted, combinators and pseudo-classes are rejected."""
        normalized = selector.strip()
        if not normalized or any(token in normalized for token in (" ", ">", "+", "~", ":", "[", "#")):
            return None
        if "." in normalized:
            tag_text, class_name = normalized.split(".", 1)
            if not class_name or "." in class_name:
                return None
            tag = tag_text.casefold() or None
            return tag, class_name, 10 + (1 if tag else 0)
        return normalized.casefold(), None, 1

    def resolve(
        self,
        element: etree._Element,
        inherited: TextStyle,
        inherited_visibility_hidden: bool = False,
    ) -> ElementStyle:
        """Calculates inherited styles for elements, label default styles, CSS rules, and inline style."""
        tag = local_name(element)
        classes = frozenset((element.get("class") or "").split())
        tag_style = TextStyle(
            bold=tag in {"b", "strong"},
            italic=tag in {"cite", "dfn", "em", "i", "var"},
            strikethrough=tag in {"del", "s", "strike"},
            superscript=tag == "sup",
            subscript=tag == "sub",
        )
        style = inherited.merge(tag_style)
        subtree_hidden = element.get("hidden") is not None or (element.get("aria-hidden") or "").casefold() == "true"
        matching: list[_SelectorCascade] = []
        if cascade := self._tag_cascades.get(tag):
            matching.append(cascade)
        for class_name in classes:
            if cascade := self._class_cascades.get(class_name):
                matching.append(cascade)
            if cascade := self._tag_class_cascades.get((tag, class_name)):
                matching.append(cascade)

        inline = _parse_declarations(element.get("style") or "")
        resolved_values: dict[str, bool] = {}
        for name in _TEXT_STYLE_FIELDS:
            candidates = [
                (important, cascade.priority, order, value)
                for cascade in matching
                if (declaration := cascade.declarations.get(name)) is not None
                for important, order, value in (declaration,)
            ]
            if (inline_declaration := inline.text.get(name)) is not None:
                important, value = inline_declaration
                candidates.append((important, 1_000, self._source_order, value))
            if candidates:
                resolved_values[name] = max(candidates, key=lambda item: item[:3])[3]
        style = TextStyleDelta(**resolved_values).apply(style)

        resolved_visibility: dict[str, bool] = {}
        for name in _VISIBILITY_FIELDS:
            candidates = [
                (important, cascade.priority, order, value)
                for cascade in matching
                if (declaration := cascade.visibility.get(name)) is not None
                for important, order, value in (declaration,)
            ]
            if (inline_declaration := inline.visibility.get(name)) is not None:
                important, value = inline_declaration
                candidates.append((important, 1_000, self._source_order, value))
            if candidates:
                resolved_visibility[name] = max(candidates, key=lambda item: item[:3])[3]
        subtree_hidden = (
            subtree_hidden or resolved_visibility.get("display", False) or resolved_visibility.get("opacity", False)
        )
        visibility_hidden = resolved_visibility.get("visibility", inherited_visibility_hidden)
        return ElementStyle(style, subtree_hidden, visibility_hidden)


__all__ = ["ElementStyle", "MarkupStylesheet", "TextStyle", "TextStyleDelta"]

# Keep the existing public type pickle path, with all old and new entries pointing to the same class.
preserve_type_module(TextStyle, "docvortex.analyzers.native._shared.markup.styles")
preserve_type_module(TextStyleDelta, "docvortex.analyzers.native._shared.markup.styles")
preserve_type_module(ElementStyle, "docvortex.analyzers.native._shared.markup.styles")
preserve_type_module(_SelectorCascade, "docvortex.analyzers.native._shared.markup.styles")
preserve_type_module(_ParsedDeclarations, "docvortex.analyzers.native._shared.markup.styles")
preserve_type_module(MarkupStylesheet, "docvortex.analyzers.native._shared.markup.styles")
