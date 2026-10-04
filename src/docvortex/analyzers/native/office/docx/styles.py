"""DOCX Rich text and style processing; shares the current single document status of Converter."""

from pathlib import Path
from typing import Any, Iterator, Optional, Union
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.xmlchemy import BaseOxmlElement
from docx.text.hyperlink import Hyperlink
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from pydantic import AnyUrl
from .formatting_types import Formatting, Script
from .....content.spans import (
    append_equation_span,
    extend_inline_spans,
    inline_span_plain_text,
    slice_span_dicts,
    strip_span_dicts,
)
from ..rich_text import (
    append_rich_text_element,
    build_spans_from_elements,
    formatting_to_style_str,
    has_non_visible_text_style,
    has_visible_style,
    normalize_format_for_text,
    should_keep_group_text,
)

from .context import _DocxConstants, _ParagraphElement


class _DocxStyles:
    """Centrally maintain rich text and styles without creating documents yourself or holding cross-document caches."""

    def _get_style_id_from_property(
        self,
        xml_element: Optional[BaseOxmlElement],
        property_tag: str,
        style_tag: str,
    ) -> Optional[str]:
        """Read style ID from the direct attribute node of paragraph or run to avoid triggering python-docx style lookup."""
        if xml_element is None:
            return None

        property_element = xml_element.find(
            property_tag,
            namespaces=_DocxConstants._BLIP_NAMESPACES,
        )
        if property_element is None:
            return None

        style_element = property_element.find(
            style_tag,
            namespaces=_DocxConstants._BLIP_NAMESPACES,
        )
        if style_element is None:
            return None

        return style_element.get(self.XML_KEY) or None

    def _get_cached_docx_style(
        self,
        part: Any,
        style_id: Optional[str],
        style_type: Any,
    ) -> Any:
        """Cache style id and python-docx style objects by type to avoid large styles.xml being repeatedly linearly scanned."""
        if part is None:
            return None

        cache_key = (style_type, style_id)
        if cache_key not in self._style_lookup_cache:
            self._style_lookup_cache[cache_key] = part.get_style(
                style_id,
                style_type,
            )
        return self._style_lookup_cache[cache_key]

    def _get_paragraph_style(self, paragraph: Optional[Paragraph]) -> Any:
        """Read paragraph styles; caches default paragraph style query results when no explicit pStyle is specified."""
        if paragraph is None:
            return None
        style_id = self._get_style_id_from_property(
            paragraph._element,
            "w:pPr",
            "w:pStyle",
        )
        return self._get_cached_docx_style(
            paragraph.part,
            style_id,
            WD_STYLE_TYPE.PARAGRAPH,
        )

    def _get_run_style(self, run: Optional[Run]) -> Any:
        """Read run character style; cache default character style query results when no explicit rStyle is specified."""
        if run is None:
            return None
        style_id = self._get_style_id_from_property(
            run._element,
            "w:rPr",
            "w:rStyle",
        )
        return self._get_cached_docx_style(
            run.part,
            style_id,
            WD_STYLE_TYPE.CHARACTER,
        )

    @staticmethod
    def _escape_hyperlink_text(text: str) -> str:
        """
        Escape square brackets in hyperlink text.

        Args:
            text: text to escape

        Returns:
            str: escaped text
        """
        if not text:
            return text
        # Escape square brackets
        text = text.replace("[", "\\[").replace("]", "\\]")
        return text

    @staticmethod
    def _escape_hyperlink_url(url: str) -> str:
        """
        Escape brackets in hyperlink URL.

        Args:
            url: URL to escape

        Returns:
            str: escaped URL
        """
        if not url:
            return url
        # URL encoding of brackets
        url = url.replace("(", "%28").replace(")", "%29")
        return url

    @staticmethod
    def _get_style_str_from_format(format_obj) -> Optional[str]:
        """
        Extracts the style string from the Formatting object.

        Args:
            format_obj: Formatting object

        Returns:
            Optional[str]: style string (such as "bold,italic"), if there is no style, return None
        """
        return formatting_to_style_str(format_obj)

    @staticmethod
    def _has_visible_style(format_obj) -> bool:
        """
        Check if the formatting contains visible styles (underline or strikethrough).

        Blank text is still visible with these styles and should be preserved.

        Args:
            format_obj: Formatting object

        Returns:
            bool: Whether to include visible styles
        """
        return has_visible_style(format_obj)

    @staticmethod
    def _has_non_visible_text_style(format_obj) -> bool:
        """Determines whether the format only has invisible text glyph styles."""
        return has_non_visible_text_style(format_obj)

    @classmethod
    def _normalize_format_for_text(
        cls,
        format_obj: Optional[Formatting],
        text: str,
        *,
        preserve_blank_non_visible_style: bool = False,
    ) -> Optional[Formatting]:
        """Converg run formatting by text content, avoid whitespace run Pass invisible styles to output.

        preserve_blank_non_visible_style for preserving whitespace within the same text fragment run
        bold/italic: These styles themselves do not make spaces visible, but may be part of consecutive style text.
        """
        return normalize_format_for_text(
            format_obj,
            text,
            preserve_blank_non_visible_style=preserve_blank_non_visible_style,
        )

    def _find_adjacent_non_blank_run_format(
        self,
        inline_contents: list[Any],
        current_index: int,
        step: int,
    ) -> Optional[Formatting]:
        """Find the nearest non-blank common run format in the adjacent direction, used to determine whether the blank run belongs to the same paragraph of style text."""
        index = current_index + step
        while 0 <= index < len(inline_contents):
            content = inline_contents[index]
            # Hyperlinks are independent output boundaries and do not borrow style context across hyperlinks.
            if isinstance(content, Hyperlink):
                return None
            if not isinstance(content, Run):
                index += step
                continue
            if self._is_hidden_run(content):
                index += step
                continue
            text = content.text or ""
            if text.strip():
                return self._get_format_from_run(content)
            index += step
        return None

    def _should_preserve_blank_non_visible_style(
        self,
        inline_contents: list[Any],
        current_index: int,
        text: str,
        format_obj: Optional[Formatting],
    ) -> bool:
        """Determines whether the bold/italic of the blank run should be retained so that consecutive text of the same style is merged into one span."""
        if not text or text.strip():
            return False
        if not self._has_non_visible_text_style(format_obj):
            return False

        previous_format = self._find_adjacent_non_blank_run_format(
            inline_contents,
            current_index,
            -1,
        )
        if format_obj == previous_format:
            return True

        next_format = self._find_adjacent_non_blank_run_format(
            inline_contents,
            current_index,
            1,
        )
        return format_obj == next_format

    @classmethod
    def _should_keep_group_text(
        cls,
        text: str,
        format_obj: Optional[Formatting],
        *,
        preserve_plain_blank: bool = False,
    ) -> bool:
        """Determine whether the current accumulated run needs to be output, retaining ordinary white space sandwiched between visible styles."""
        return should_keep_group_text(
            text,
            format_obj,
            preserve_plain_blank=preserve_plain_blank,
        )

    @staticmethod
    def _append_paragraph_element(
        paragraph_elements: list[tuple[str, Optional[Formatting], Optional[Union[AnyUrl, Path, str]]]],
        text: str,
        format_obj: Optional[Formatting],
        hyperlink: Optional[Union[AnyUrl, Path, str]],
    ) -> None:
        """Append paragraph elements; adjacent run with the same hyperlink and the same format are merged into one element."""
        append_rich_text_element(paragraph_elements, text, format_obj, hyperlink)

    @staticmethod
    def _normalize_hyperlink_group_boundaries(
        paragraph_elements: list[_ParagraphElement],
    ) -> list[_ParagraphElement]:
        """Move the link group boundary blank to normal text, cut only the beginning and end of the entire paragraph, and retain the blank space within the group."""

        output: list[_ParagraphElement] = []
        index = 0
        while index < len(paragraph_elements):
            element = paragraph_elements[index]
            hyperlink = element[2]
            if hyperlink is None:
                output.append(element)
                index += 1
                continue
            group_end = index + 1
            while (
                group_end < len(paragraph_elements)
                and paragraph_elements[group_end][2] is not None
                and str(paragraph_elements[group_end][2]) == str(hyperlink)
            ):
                group_end += 1
            group = list(paragraph_elements[index:group_end])
            first_text, first_format, first_hyperlink = group[0]
            last_text, last_format, last_hyperlink = group[-1]
            if len(group) == 1 and not first_text.strip():
                leading_space = ""
                trailing_space = first_text
                group[0] = ("", first_format, first_hyperlink)
            else:
                leading_length = len(first_text) - len(first_text.lstrip())
                trailing_length = len(last_text) - len(last_text.rstrip())
                leading_space = first_text[:leading_length]
                trailing_space = last_text[len(last_text) - trailing_length :] if trailing_length else ""
                if len(group) == 1:
                    group[0] = (
                        first_text[leading_length : len(first_text) - trailing_length if trailing_length else len(first_text)],
                        first_format,
                        first_hyperlink,
                    )
                else:
                    group[0] = (
                        first_text[leading_length:],
                        first_format,
                        first_hyperlink,
                    )
                    group[-1] = (
                        last_text[: len(last_text) - trailing_length] if trailing_length else last_text,
                        last_format,
                        last_hyperlink,
                    )
            if leading_space and output:
                output.append((leading_space, first_format, None))
            output.extend(item for item in group if item[0])
            if trailing_space and group_end < len(paragraph_elements):
                output.append((trailing_space, last_format, None))
            index = group_end
        return output

    @staticmethod
    def _is_hidden_run(run: Run) -> bool:
        """Check whether a run is marked as hidden text in Word."""
        _W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        rpr = run._element.find(f"{{{_W}}}rPr")
        if rpr is None:
            return False
        # webHidden: commonly used by TOC page-number field runs
        if rpr.find(f"{{{_W}}}webHidden") is not None:
            return True
        # vanish: generic hidden text
        if rpr.find(f"{{{_W}}}vanish") is not None:
            return True
        return False

    @classmethod
    def _build_spans_from_elements(
        cls,
        paragraph_elements: list[tuple[str, Optional[Formatting], Optional[Union[AnyUrl, Path, str]]]],
    ) -> list[dict[str, Any]]:
        """Group by consecutive URL hyperlink to directly generate structured Span."""
        return build_spans_from_elements(paragraph_elements)

    def _build_text_from_elements(
        self,
        paragraph_elements: list[tuple[str, Optional[Formatting], Optional[Union[AnyUrl, Path, str]]]],
    ) -> list[dict[str, Any]]:
        """
        Restructure text from paragraph_elements, applying hyperlink formatting and font styles.

        Args:
            paragraph_elements: List of paragraph elements

        Returns:
            list[dict]: Reorganized Span
        """
        return self._build_spans_from_elements(paragraph_elements)

    @staticmethod
    def _normalize_text_block_content(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Normalize plain text block export content.

        DOCX Commonly used spaces at the beginning and end of paragraphs simulate layout alignment, and remove these leading and trailing spaces before exporting ordinary text blocks.
        """
        return strip_span_dicts(content)

    def _build_text_with_equations_and_hyperlinks(
        self,
        paragraph_elements: list[tuple[str, Optional[Formatting], Optional[Union[AnyUrl, Path, str]]]],
        text_with_equations: str,
        equations: list[tuple[str, str]],
    ) -> list[dict[str, Any]]:
        """
        Construct text that includes formulas, hyperlinks, and font styles.

        Args:
            paragraph_elements: List of paragraph elements, including formatting and hyperlink information
            text_with_equations: Original visible text without formulas
            equations: text/equation token in order of source

        Returns:
            list[dict]: Span containing formulas, hyperlinks and font styles
        """
        if not equations:
            return self._build_text_from_elements(paragraph_elements)
        styled_spans = self._build_text_from_elements(paragraph_elements)
        styled_visible = inline_span_plain_text(styled_spans)
        plain_visible = "".join(value for kind, value in equations if kind == "text")
        if plain_visible != styled_visible:
            return styled_spans

        output: list[dict[str, Any]] = []
        visible_cursor = 0
        for kind, value in equations:
            if kind == "equation":
                append_equation_span(output, value)
                continue
            next_cursor = visible_cursor + len(value)
            extend_inline_spans(output, slice_span_dicts(styled_spans, visible_cursor, next_cursor))
            visible_cursor = next_cursor
        return strip_span_dicts(output)

    @staticmethod
    def _get_paragraph_text_from_contents(
        inner_contents: list[Union[Run, Hyperlink]],
    ) -> str:
        """Rebuild paragraph plain text from visible inline containers."""
        return "".join(content.text or "" for content in inner_contents)

    def _get_paragraph_text(self, paragraph: Paragraph) -> str:
        """Return paragraph plain text, including inline ``w:sdt`` content."""
        return self._get_paragraph_text_from_contents(list(self._iter_paragraph_inner_content(paragraph)))

    def _resolve_style_chain_bool(
        self,
        style_obj,
        attr_name: str,
    ) -> Optional[bool]:
        """Resolve boolean font properties from the style inheritance chain."""
        if style_obj is None:
            return None

        cache_key = (id(style_obj), attr_name)
        if cache_key in self._style_bool_cache:
            return self._style_bool_cache[cache_key]

        style = style_obj
        result = None
        visited_styles = set()
        while style is not None:
            style_element = getattr(style, "_element", None)
            style_id = getattr(style, "style_id", None)
            style_type = getattr(style, "type", None)
            # DOCX There may be basedOn self-reference or circular reference, record the accessed style to avoid inheritance chain infinite loop.
            style_marker = (
                id(style_element) if style_element is not None else id(style),
                style_type,
                style_id,
            )
            if style_marker in visited_styles:
                break
            visited_styles.add(style_marker)

            font = getattr(style, "font", None)
            if font is not None:
                if attr_name == "underline":
                    value = font.underline
                elif attr_name == "strikethrough":
                    value = font.strike
                else:
                    value = getattr(font, attr_name, None)
                if value is not None:
                    result = bool(value)
                    break
            style = getattr(style, "base_style", None)
        self._style_bool_cache[cache_key] = result
        return result

    def _resolve_run_bool_with_inheritance(
        self,
        run: Run,
        attr_name: str,
    ) -> bool:
        """Parse the font attributes of run and support run/character style/paragraph style inheritance."""
        if attr_name == "underline":
            direct_value = run.underline
        elif attr_name == "strikethrough":
            direct_value = run.font.strike
        else:
            direct_value = getattr(run, attr_name, None)

        if direct_value is not None:
            return bool(direct_value)

        # First look at the run level character style chain (skip the Hyperlink default character style to avoid using the default underline
        # Mistakenly regarded as text emphasis style and injected into the parsing result)
        run_style = self._get_run_style(run)
        run_style_id = str(getattr(run_style, "style_id", "") or "").lower()
        run_style_name = str(getattr(run_style, "name", "") or "").lower()
        is_hyperlink_style = run_style_id == "hyperlink" or "hyperlink" in run_style_name
        if not is_hyperlink_style:
            inherited = self._resolve_style_chain_bool(run_style, attr_name)
            if inherited is not None:
                return inherited

        # Look at the paragraph style chain
        parent = getattr(run, "_parent", None)
        inherited = self._resolve_style_chain_bool(
            self._get_paragraph_style(parent),
            attr_name,
        )
        if inherited is not None:
            return inherited

        return False

    @staticmethod
    def _get_direct_underline_style(run: Run) -> str:
        """Read the run level underline type, used to distinguish words type of underline that does not act on spaces."""
        _W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        rPr = run._element.find(f"{{{_W}}}rPr")
        if rPr is None:
            return ""
        underline = rPr.find(f"{{{_W}}}u")
        if underline is None:
            return ""
        return underline.get(f"{{{_W}}}val", "single")

    def _get_format_from_run(self, run: Run) -> Optional[Formatting]:
        """
        Get format information from the Run object.

        Args:
            run: Run object

        Returns:
            Optional[Formatting]: format object
        """
        is_bold = self._resolve_run_bool_with_inheritance(run, "bold")
        is_italic = self._resolve_run_bool_with_inheritance(run, "italic")
        is_strikethrough = self._resolve_run_bool_with_inheritance(run, "strikethrough")
        is_underline = self._resolve_run_bool_with_inheritance(run, "underline")
        underline_style = self._get_direct_underline_style(run)

        # Detect underscores (w:em): Reserved independently as emphasis to avoid confusion with real underscores.
        is_emphasis = False
        _W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        rPr = run._element.find(f"{{{_W}}}rPr")
        if rPr is not None:
            em = rPr.find(f"{{{_W}}}em")
            if em is not None:
                em_val = em.get(f"{{{_W}}}val", "")
                if em_val and em_val != "none":
                    is_emphasis = True

        is_sub = run.font.subscript or False
        is_sup = run.font.superscript or False
        script = Script.SUB if is_sub else Script.SUPER if is_sup else Script.BASELINE

        return Formatting(
            bold=is_bold,
            italic=is_italic,
            underline=is_underline,
            underline_style=underline_style,
            emphasis=is_emphasis,
            strikethrough=is_strikethrough,
            script=script,
        )

    def _handle_equations_in_text(
        self,
        element: Any,
        text: str,
        *,
        part: Any | None = None,
    ) -> tuple[str, list[tuple[str, str]]]:
        """
        Manipulate formulas in text.

        Args:
            element: element object
            text: Text content
            part: OOXML part to which the current paragraph belongs, used to parse local relationship

        Returns:
            tuple: (original visible text, ordered with formulas text/equation token)
        """
        source_part = part or self._require_document_part()
        only_texts: list[str] = []
        formula_values: list[str] = []
        tokens: list[tuple[str, str]] = []
        for token_kind, value in self._docx_formula_tokens(element, source_part):
            if token_kind == "text":
                only_texts.append(value)
                tokens.append(("text", value))
                continue
            formula_values.append(value)
            tokens.append(("equation", value))

        if not formula_values:
            return text, []

        if "".join(only_texts) != text:
            # If we cannot reconstruct the initial raw text
            # Don't try to parse the formula and return the original text
            return text, []

        return text, tokens

    def _get_label_and_level(self, paragraph: Paragraph) -> tuple[str, Optional[int]]:
        """
        Get the paragraph label and level.

        Args:
            paragraph: paragraph object

        Returns:
            tuple[str, Optional[int]]: (label, level) tuple
        """
        paragraph_style = self._get_paragraph_style(paragraph)
        if paragraph_style is None:
            return "Normal", None

        label = paragraph_style.style_id
        name = paragraph_style.name

        if label is None:
            return "Normal", None

        for style in self._iter_style_chain(paragraph_style):
            style_label = getattr(style, "style_id", None)
            style_name = getattr(style, "name", None)

            if style_label and ":" in style_label:
                parts = style_label.split(":")
                if len(parts) == 2:
                    return parts[0], self._str_to_int(parts[1], None)

            for candidate in (style_label, style_name):
                if candidate and "heading" in candidate.lower():
                    return self._get_heading_and_level(candidate)

        outline_level = self._get_effective_outline_level(paragraph)
        if outline_level is not None:
            return "Heading", outline_level + 1

        return name or label or "Normal", None

    def _iter_style_chain(self, style: Any) -> Iterator[Any]:
        """Yield a style and its base-style chain once each."""
        seen: set[int] = set()
        current = style
        while current is not None:
            current_id = id(current)
            if current_id in seen:
                break
            seen.add(current_id)
            yield current
            current = getattr(current, "base_style", None)

    def _get_paragraph_property_child(
        self, xml_element: Optional[BaseOxmlElement], child_tag: str
    ) -> Optional[BaseOxmlElement]:
        """Read a direct child from w:pPr without matching nested descendants."""
        if xml_element is None:
            return None

        namespaces = getattr(xml_element, "nsmap", None) or _DocxConstants._BLIP_NAMESPACES
        pPr = xml_element.find("w:pPr", namespaces=namespaces)
        if pPr is None:
            return None
        return pPr.find(child_tag, namespaces=namespaces)

    def _get_effective_numPr(self, paragraph: Paragraph) -> Optional[BaseOxmlElement]:
        """Resolve paragraph numbering from direct properties, then style inheritance."""
        numPr = self._get_paragraph_property_child(paragraph._element, "w:numPr")
        if numPr is not None:
            return numPr

        for style in self._iter_style_chain(self._get_paragraph_style(paragraph)):
            style_element = getattr(style, "element", None)
            numPr = self._get_paragraph_property_child(style_element, "w:numPr")
            if numPr is not None:
                return numPr

        return None

    def _get_effective_outline_level(self, paragraph: Paragraph) -> Optional[int]:
        """Resolve outline level from paragraph properties or inherited styles."""
        outline_lvl = self._get_paragraph_property_child(paragraph._element, "w:outlineLvl")
        if outline_lvl is None:
            for style in self._iter_style_chain(self._get_paragraph_style(paragraph)):
                style_element = getattr(style, "element", None)
                outline_lvl = self._get_paragraph_property_child(style_element, "w:outlineLvl")
                if outline_lvl is not None:
                    break

        if outline_lvl is None:
            return None

        return self._str_to_int(outline_lvl.get(self.XML_KEY), None)
