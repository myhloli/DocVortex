"""Project static XHTML/HTML DOM to DocVortex raw blocks."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Protocol, TypeAlias

from lxml import etree  # type: ignore[reportMissingImports]

from docvortex.content.markup.formula import FormulaExtraction, extract_formula
from docvortex.content.markup.styles import MarkupStylesheet, TextStyle
from docvortex.content.spans import (
    append_code_span,
    append_equation_span,
    append_hyperlink_span,
    append_text_span,
    extend_inline_spans,
    inline_span_plain_text,
    strip_span_dicts,
    text_spans,
)
from docvortex.schema import RAW_ALGORITHM, VISUAL_TYPE_MAPPING, BlockType

from ...foundation.type_identity import preserve_type_module
from ...foundation.xml_names import local_name

BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "body",
        "dd",
        "details",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "main",
        "math",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "svg",
        "table",
        "ul",
    }
)
SKIPPED_TAGS = frozenset(
    {
        "audio",
        "button",
        "canvas",
        "embed",
        "form",
        "head",
        "iframe",
        "input",
        "noscript",
        "object",
        "script",
        "select",
        "style",
        "template",
        "textarea",
        "video",
    }
)
_WHITESPACE_RE = re.compile(r"[\t\r\n\f ]+")
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
_MAX_TABLE_SPAN = 1_000
_CAPTION_TOKENS = frozenset(
    {
        "caption",
        "figure-caption",
        "image-caption",
        "table-caption",
        "chart-caption",
        "code-caption",
        "docvortex-caption",
    }
)
_FOOTNOTE_TOKENS = frozenset(
    {
        "footnote",
        "figure-footnote",
        "image-footnote",
        "table-footnote",
        "chart-footnote",
        "code-footnote",
        "docvortex-footnote",
    }
)
_VISUAL_ELEMENT_TAGS = frozenset({"img", "image", "pre", "svg", "table"})
_LIST_PAGE_BLOCK_TAGS = frozenset({"figure", "image", "img", "math", "pre", "svg", "table"})
_InlineSpanDict: TypeAlias = dict[str, object]
_InlineProjectionSegment: TypeAlias = list[_InlineSpanDict] | dict[str, object]


class _SourceSpans(list[_InlineSpanDict]):
    """The source element ownership is only carried during the projection period, removed before materialization, and does not enter the public agreement."""

    def __init__(self, spans: list[_InlineSpanDict] | None = None) -> None:
        """Copy inline content and existing sources to avoid losing positioning information when merging Span."""
        super().__init__(spans or [])
        self.sources: set[etree._Element] = set(spans.sources) if isinstance(spans, _SourceSpans) else set()

    def clear(self) -> None:
        """The source is cleared synchronously after the buffer is submitted to prevent subsequent fragments from inheriting references from the previous fragment."""
        super().clear()
        self.sources.clear()


def _has_source_id(element: etree._Element) -> bool:
    """Only track elements with explicit identities to avoid saving useless DOM sources when the text is large."""
    return bool(element.get("id") or element.get("{http://www.w3.org/XML/1998/namespace}id"))


def _extend_source_spans(output: list[_InlineSpanDict], spans: list[_InlineSpanDict]) -> None:
    """Propagating sources outside of normal inline merging does not modify the semantic fields of Span."""
    if isinstance(output, _SourceSpans) and isinstance(spans, _SourceSpans):
        output.sources.update(spans.sources)
    extend_inline_spans(output, spans)


def _strip_source_spans(spans: list[_InlineSpanDict]) -> list[_InlineSpanDict]:
    """Sources of valid fragments are retained after trimming text, empty fragments do not produce targets."""
    content = strip_span_dicts(spans)
    if content and isinstance(spans, _SourceSpans) and spans.sources:
        result = _SourceSpans(content)
        result.sources.update(spans.sources)
        return result
    return content


def clean_text_node(value: str | None) -> str:
    """Collapses typographic whitespace in plain markup document text nodes."""
    return _WHITESPACE_RE.sub(" ", value) if value else ""


def visible_text(element: etree._Element) -> str:
    """Extracts the visible plain text of an element after whitespace is collapsed."""
    return _WHITESPACE_RE.sub(" ", html.unescape("".join(element.itertext()))).strip()


def _semantic_tokens(element: etree._Element) -> frozenset[str]:
    """Complete blank token by class/id Returns the lowercase collection without performing any substring matching."""
    value = f"{element.get('class') or ''} {element.get('id') or ''}".casefold()
    return frozenset(value.split())


def _raw_visual_type(value: object) -> BlockType | None:
    """Normalize raw visual body or algorithm to a unified parent block type."""
    if value in {BlockType.IMAGE, BlockType.TABLE, BlockType.CHART}:
        return BlockType(value)
    if value in {BlockType.CODE, RAW_ALGORITHM}:
        return BlockType.CODE
    return None


def _append_inline_segment(
    segments: list[_InlineProjectionSegment],
    segment: _InlineProjectionSegment,
) -> None:
    """Append inline projection fragments and merge adjacent Span groups to maintain stable block granularity."""
    if isinstance(segment, list) and segments and isinstance(segments[-1], list):
        _extend_source_spans(segments[-1], segment)
    elif not isinstance(segment, list) or segment:
        segments.append(_SourceSpans(segment) if isinstance(segment, list) else segment)


def _append_list_block_content(parts: list[_InlineSpanDict], rendered: list[_InlineSpanDict]) -> None:
    """Use line breaks to surround block-level text within list items to prevent adjacent paragraphs from silently sticking together."""
    if not rendered:
        return
    last_visible = inline_span_plain_text(parts)
    if last_visible and not last_visible.endswith("\n"):
        append_text_span(parts, "\n")
    _extend_source_spans(parts, rendered)
    append_text_span(parts, "\n")


def entity_text(element: etree._Element) -> str:
    """Restore lxml reserved security named entities to visible text."""
    name = getattr(element, "name", "")
    return html.unescape(f"&{name};") if name else ""


def bounded_table_span(value: str) -> str | None:
    """Normalize bounded table spans to avoid unusual integer scaling of rendering grids."""
    if not value.isdigit():
        return None
    normalized = value.lstrip("0")
    if not normalized or len(normalized) > len(str(_MAX_TABLE_SPAN)):
        return None
    span = int(normalized)
    return str(span) if span <= _MAX_TABLE_SPAN else None


def visible_raw_text_with_style(
    element: etree._Element,
    stylesheet: MarkupStylesheet,
    style: TextStyle,
    visibility_hidden: bool,
) -> str:
    """Recursive extraction respects whole-tree hiding and inherits the original text of visibility."""
    parts: list[str] = [] if visibility_hidden else [element.text or ""]
    for child in element:
        if isinstance(child.tag, str):
            resolved = stylesheet.resolve(child, style, visibility_hidden)
            if not resolved.subtree_hidden:
                parts.append(
                    visible_raw_text_with_style(
                        child,
                        stylesheet,
                        resolved.text,
                        resolved.visibility_hidden,
                    )
                )
        elif not visibility_hidden:
            parts.append(entity_text(child))
        if not visibility_hidden:
            parts.append(child.tail or "")
    return "".join(parts)


@dataclass(frozen=True, slots=True)
class ResolvedMarkupImage:
    """Save the mutually exclusive payload and description text after parsing the marked document image."""

    image_base64: str | None = None
    image_url: str | None = None
    alt: str = ""


class MarkupContext(Protocol):
    """Define projector to request links, images, and boundaries for anchor from a specific container."""

    def resolve_link(self, href: str) -> str | None:
        """Resolve a safe link target."""

    def resolve_image(self, source: str, *, alt: str = "") -> ResolvedMarkupImage | None:
        """Resolves image as data URI, remote URL, or visible degraded text."""

    def heading_anchor(self, heading: etree._Element) -> str | None:
        """Returns the specification anchor corresponding to the title."""

    def heading_label(self, anchor: str) -> str | None:
        """Returns the title tag corresponding to specification anchor."""

    def note_anchor(self, note: etree._Element) -> str | None:
        """Returns the specification anchor corresponding to the footnote node."""


class MarkupProjector:
    """Project a static content root node into unity raw blocks in order DOM."""

    def __init__(
        self,
        root: etree._Element,
        context: MarkupContext,
        stylesheet: MarkupStylesheet,
        *,
        single_document_title: bool = False,
        document_title_emitted: bool = False,
        track_text_sources: bool = False,
    ) -> None:
        """Binds DOM, format adapter, limited CSS and title policy."""
        self.track_text_sources = track_text_sources
        self.text_sources: list[tuple[etree._Element, dict[str, object]]] = []
        self.root = root
        self.context = context
        self.stylesheet = stylesheet
        self.single_document_title = single_document_title
        self.document_title_emitted = document_title_emitted

    def convert(self) -> list[dict[str, object]]:
        """Transforms the subtree of the content root node and returns raw blocks in order DOM."""
        resolved = self.stylesheet.resolve(self.root, TextStyle())
        if resolved.subtree_hidden:
            return []
        name = local_name(self.root)
        if name == "figure" or (name in {"aside", "div", "section"} and self._has_contextual_visual_annotation(self.root)):
            blocks = self._parse_figure(self.root, resolved.text, resolved.visibility_hidden)
        else:
            blocks = self._parse_container_contents(self.root, resolved.text, resolved.visibility_hidden)
        self._bind_container_source(self.root, blocks, resolved.visibility_hidden)
        self.text_sources.clear()
        for block in blocks:
            content = block.get("content")
            if isinstance(content, _SourceSpans):
                if block.get("type") == BlockType.TEXT:
                    self.text_sources.extend((source, block) for source in content.sources)
                block["content"] = list(content)
        # Sources are only used to register top-level text objects; nested lists with Span must also restore ordinary JSON containers.
        pending = list(blocks)
        while pending:
            item = pending.pop()
            content = item.get("content")
            if isinstance(content, list):
                item["content"] = list(content)
                pending.extend(child for child in content if isinstance(child, dict))
        return blocks

    def convert_svg(self) -> list[dict[str, object]]:
        """Do your best to convert the standalone SVG root node into text and static images."""
        resolved = self.stylesheet.resolve(self.root, TextStyle())
        return [] if resolved.subtree_hidden else self._parse_svg(self.root, resolved.text, resolved.visibility_hidden)

    def project_block(self, element: etree._Element) -> list[dict[str, object]]:
        """Project a known block element according to the default inheritance style for versioned HTML decoding and reuse."""
        return self._parse_block(element, TextStyle())

    def project_inline_content(self, element: etree._Element) -> list[_InlineSpanDict]:
        """Restore a known in-line container to structured Span."""
        resolved = self.stylesheet.resolve(element, TextStyle())
        if resolved.subtree_hidden:
            return []
        content, extras = self._render_inline_children(element, resolved.text, resolved.visibility_hidden)
        if extras:
            raise ValueError("inline projection produced unexpected block content")
        return _strip_source_spans(content)

    def _parse_container_contents(
        self,
        element: etree._Element,
        style: TextStyle,
        visibility_hidden: bool = False,
    ) -> list[dict[str, object]]:
        """Split consecutive inline content and block-level child elements into raw blocks in source order."""
        blocks: list[dict[str, object]] = []
        inline_parts: list[_InlineSpanDict] = _SourceSpans()
        if not visibility_hidden:
            _extend_source_spans(inline_parts, self._render_text(element.text, style))

        def flush_inline() -> None:
            """Write the current continuous inline segment as normal text block."""
            content = _strip_source_spans(inline_parts)
            inline_parts.clear()
            if content:
                blocks.append({"type": BlockType.TEXT, "content": content})

        for child in element:
            if not isinstance(child.tag, str):
                if not visibility_hidden:
                    _extend_source_spans(inline_parts, self._render_text(entity_text(child), style))
                    _extend_source_spans(inline_parts, self._render_text(child.tail, style))
                continue
            name = local_name(child)
            if name in BLOCK_TAGS:
                flush_inline()
                blocks.extend(self._parse_block(child, style, visibility_hidden))
            else:
                for segment in self._render_inline_element_ordered(child, style, visibility_hidden):
                    if isinstance(segment, list):
                        _extend_source_spans(inline_parts, segment)
                    else:
                        flush_inline()
                        blocks.append(segment)
            if not visibility_hidden:
                _extend_source_spans(inline_parts, self._render_text(child.tail, style))
        flush_inline()
        return blocks

    def _bind_container_source(
        self,
        element: etree._Element,
        blocks: list[dict[str, object]],
        hidden: bool,
    ) -> None:
        """Bind the visible container itself to the first text segment, and do not bind the internal ID to the previous segment by mistake."""
        if not self.track_text_sources or hidden or not _has_source_id(element):
            return
        for block in blocks:
            content = block.get("content")
            if block.get("type") == BlockType.TEXT and isinstance(content, list) and content:
                tracked = _SourceSpans(content)
                tracked.sources.add(element)
                block["content"] = tracked
                return

    def _parse_block(
        self,
        element: etree._Element,
        inherited: TextStyle,
        inherited_visibility_hidden: bool = False,
    ) -> list[dict[str, object]]:
        """Record container position after block projection is complete, only EPUB explicitly enables source tracking."""
        blocks = self._parse_block_content(element, inherited, inherited_visibility_hidden)
        if not self.track_text_sources:
            return blocks
        resolved = self.stylesheet.resolve(element, inherited, inherited_visibility_hidden)
        if not resolved.subtree_hidden:
            self._bind_container_source(element, blocks, resolved.visibility_hidden)
        return blocks

    def _parse_block_content(
        self,
        element: etree._Element,
        inherited: TextStyle,
        inherited_visibility_hidden: bool = False,
    ) -> list[dict[str, object]]:
        """Assign a block-level element to the corresponding raw block conversion logic."""
        resolved = self.stylesheet.resolve(element, inherited, inherited_visibility_hidden)
        if resolved.subtree_hidden:
            return []
        name = local_name(element)
        if name in SKIPPED_TAGS or name == "hr":
            return []
        if self.context.note_anchor(element) is not None:
            return self._parse_note_element(element, resolved.text, resolved.visibility_hidden)
        if name in {"h1", "h2", "h3", "h4", "h5", "h6", "p"}:
            return self._parse_textual_block(element, name, resolved.text, resolved.visibility_hidden)
        if name in {"ul", "ol"}:
            list_block, extras = self._parse_list(element, resolved.text, resolved.visibility_hidden)
            return ([list_block] if list_block is not None else []) + extras
        if name == "table":
            return self._parse_table(element, resolved.text, resolved.visibility_hidden)
        if name == "pre":
            content = self._visible_raw_text(element, resolved.text, resolved.visibility_hidden)
            language = self._code_language_hint(element)
            block: dict[str, object] = {"type": BlockType.CODE, "content": content}
            if language:
                block["guess_lang"] = language
            return [block] if content.strip() else []
        if name == "math":
            if resolved.visibility_hidden:
                return []
            formula = self._formula_extraction(element)
            if formula is not None:
                return [{"type": BlockType.EQUATION, "content": formula.latex}]
            fallback = self._visible_plain_text(element, resolved.text, resolved.visibility_hidden)
            return [{"type": BlockType.TEXT, "content": text_spans(fallback)}] if fallback else []
        if name == "figure":
            return self._parse_figure(element, resolved.text, resolved.visibility_hidden)
        if name in {"aside", "div", "section"} and self._has_contextual_visual_annotation(element):
            return self._parse_figure(element, resolved.text, resolved.visibility_hidden)
        if name == "svg":
            return self._parse_svg(element, resolved.text, resolved.visibility_hidden)
        return self._parse_container_contents(element, resolved.text, resolved.visibility_hidden)

    def _parse_textual_block(
        self,
        element: etree._Element,
        name: str,
        style: TextStyle,
        visibility_hidden: bool,
    ) -> list[dict[str, object]]:
        """Convert a title or paragraph and bypass the visual within it blocks."""
        blocks: list[dict[str, object]] = []
        text_emitted = False
        for segment in self._render_inline_children_ordered(element, style, visibility_hidden):
            if not isinstance(segment, list):
                blocks.append(segment)
                continue
            content = _strip_source_spans(segment)
            if self.track_text_sources and _has_source_id(element) and not visibility_hidden and content and not text_emitted:
                content = _SourceSpans(content)
                content.sources.add(element)
            if not content:
                continue
            if text_emitted:
                blocks.append({"type": BlockType.TEXT, "content": content})
                continue
            if name == "h1" and (not self.single_document_title or not self.document_title_emitted):
                block: dict[str, object] = {"type": BlockType.DOC_TITLE, "level": 1, "content": content}
                self.document_title_emitted = True
            elif name.startswith("h"):
                level = min(max(int(name[1:]), 2), 6)
                block = {
                    "type": BlockType.PARAGRAPH_TITLE,
                    "level": level,
                    "is_numbered_style": False,
                    "content": content,
                }
            else:
                block = {"type": BlockType.TEXT, "content": content}
            if name.startswith("h") and (anchor := self.context.heading_anchor(element)):
                block["anchor"] = anchor
            blocks.append(block)
            text_emitted = True
        return blocks

    def _parse_note_element(
        self,
        element: etree._Element,
        style: TextStyle,
        visibility_hidden: bool = False,
    ) -> list[dict[str, object]]:
        """Convert individual footnotes block by block and mount anchor only to the first text footnote."""
        blocks = self._parse_container_contents(element, style, visibility_hidden)
        anchor = self.context.note_anchor(element)
        anchor_attached = False
        for block in blocks:
            content = block.get("content")
            if block.get("type") != BlockType.TEXT or not isinstance(content, list) or not content:
                continue
            block["type"] = BlockType.PAGE_FOOTNOTE
            if anchor is not None and not anchor_attached:
                block["anchor"] = anchor
                anchor_attached = True
        return blocks

    def _render_inline_children(
        self,
        element: etree._Element,
        style: TextStyle,
        visibility_hidden: bool = False,
    ) -> tuple[list[_InlineSpanDict], list[dict[str, object]]]:
        """Renders the element's contiguous inline Span and bypasses the visual blocks within it."""
        segments = self._render_inline_children_ordered(element, style, visibility_hidden)
        content: list[_InlineSpanDict] = _SourceSpans()
        for segment in segments:
            if isinstance(segment, list):
                _extend_source_spans(content, segment)
        return (
            content,
            [segment for segment in segments if not isinstance(segment, list)],
        )

    def _render_inline_children_ordered(
        self,
        element: etree._Element,
        style: TextStyle,
        visibility_hidden: bool = False,
    ) -> list[_InlineProjectionSegment]:
        """Returns continuous text in DOM order with bypass block, preserving inline visual front and rear boundaries."""
        segments: list[_InlineProjectionSegment] = []
        if not visibility_hidden:
            _append_inline_segment(segments, self._render_text(element.text, style))
        for child in element:
            if not isinstance(child.tag, str):
                if not visibility_hidden:
                    _append_inline_segment(segments, self._render_text(entity_text(child), style))
                    _append_inline_segment(segments, self._render_text(child.tail, style))
                continue
            for segment in self._render_inline_element_ordered(child, style, visibility_hidden):
                _append_inline_segment(segments, segment)
            if not visibility_hidden:
                _append_inline_segment(segments, self._render_text(child.tail, style))
        return segments

    def _render_inline_element(
        self,
        element: etree._Element,
        inherited: TextStyle,
        inherited_visibility_hidden: bool = False,
    ) -> tuple[list[_InlineSpanDict], list[dict[str, object]]]:
        """Convert an inline element to a structured Span and optional visual block."""
        segments = self._render_inline_element_ordered(element, inherited, inherited_visibility_hidden)
        content: list[_InlineSpanDict] = _SourceSpans()
        for segment in segments:
            if isinstance(segment, list):
                _extend_source_spans(content, segment)
        return (
            content,
            [segment for segment in segments if not isinstance(segment, list)],
        )

    def _render_inline_element_ordered(
        self,
        element: etree._Element,
        inherited: TextStyle,
        inherited_visibility_hidden: bool = False,
    ) -> list[_InlineProjectionSegment]:
        """Records the first visible text segment within the line where ID actually resides, preserving visual segmentation boundaries."""
        segments = self._render_inline_element_segments(element, inherited, inherited_visibility_hidden)
        if self.track_text_sources and _has_source_id(element):
            resolved = self.stylesheet.resolve(element, inherited, inherited_visibility_hidden)
            if not resolved.subtree_hidden and not resolved.visibility_hidden:
                for index, segment in enumerate(segments):
                    if isinstance(segment, list) and inline_span_plain_text(segment).strip():
                        tracked = _SourceSpans(segment)
                        tracked.sources.add(element)
                        segments[index] = tracked
                        break
        return segments

    def _render_inline_element_segments(
        self,
        element: etree._Element,
        inherited: TextStyle,
        inherited_visibility_hidden: bool = False,
    ) -> list[_InlineProjectionSegment]:
        """Recursively projects a single inline element, preserving sequential segmentation at nested visual positions."""
        resolved = self.stylesheet.resolve(element, inherited, inherited_visibility_hidden)
        if resolved.subtree_hidden:
            return []
        name = local_name(element)
        if name in SKIPPED_TAGS:
            return []
        if name == "br":
            return [] if resolved.visibility_hidden else [text_spans("\n")]
        if name in {"img", "image"}:
            return [] if resolved.visibility_hidden else self._image_blocks(element)
        if name == "math":
            if resolved.visibility_hidden:
                return []
            formula = self._formula_extraction(element)
            if formula is not None:
                if formula.display == "block":
                    return [{"type": BlockType.EQUATION, "content": formula.latex}]
                spans: list[_InlineSpanDict] = _SourceSpans()
                append_equation_span(spans, formula.latex)
                return [spans]
            fallback = self._visible_plain_text(element, resolved.text, resolved.visibility_hidden)
            return [text_spans(fallback)] if fallback else []
        if name == "code":
            if resolved.visibility_hidden:
                return []
            code = self._visible_raw_text(element, resolved.text, resolved.visibility_hidden)
            spans = []
            append_code_span(spans, code)
            return [spans] if spans else []
        if name in BLOCK_TAGS:
            return self._parse_block(element, inherited, inherited_visibility_hidden)
        segments = self._render_inline_children_ordered(element, resolved.text, resolved.visibility_hidden)
        if name == "a":
            href = element.get("href") or element.get(_XLINK_HREF) or ""
            target = self.context.resolve_link(href)
            if target:
                linked: list[_InlineProjectionSegment] = []
                for segment in segments:
                    if not isinstance(segment, list) or not segment:
                        linked.append(segment)
                        continue
                    wrapped: list[_InlineSpanDict] = _SourceSpans()
                    append_hyperlink_span(wrapped, segment, target)
                    if isinstance(segment, _SourceSpans) and isinstance(wrapped, _SourceSpans):
                        wrapped.sources.update(segment.sources)
                    linked.append(wrapped)
                return linked
        return segments

    @staticmethod
    def _render_text(value: str | None, style: TextStyle) -> list[_InlineSpanDict]:
        """Collapse the text node and project directly to styled TextSpan."""
        text = clean_text_node(value)
        if not text:
            return []
        return text_spans(text, style.names())

    def _visible_raw_text(
        self,
        element: etree._Element,
        style: TextStyle,
        visibility_hidden: bool = False,
    ) -> str:
        """Recursively extract the visible original text and allow posterity to explicitly restore visibility."""
        return visible_raw_text_with_style(element, self.stylesheet, style, visibility_hidden)

    def _visible_plain_text(
        self,
        element: etree._Element,
        style: TextStyle,
        visibility_hidden: bool = False,
    ) -> str:
        """Returns the visible plain text after collapsing whitespace and restoring entities."""
        value = self._visible_raw_text(element, style, visibility_hidden)
        return _WHITESPACE_RE.sub(" ", html.unescape(value)).strip()

    def _image_blocks(
        self,
        element: etree._Element,
        *,
        caption: str | None = None,
        emit_alt_caption: bool = True,
    ) -> list[dict[str, object]]:
        """Convert the parsable image to image block and add the description with caption/alt."""
        source = element.get("src") or element.get("href") or element.get(_XLINK_HREF) or ""
        requested_alt = (caption or element.get("alt") or element.get("title") or "").strip()
        resolved = self.context.resolve_image(source, alt=requested_alt)
        alt = (resolved.alt if resolved is not None else requested_alt).strip()
        if resolved is None or not (resolved.image_base64 or resolved.image_url):
            return [{"type": BlockType.TEXT, "content": text_spans(alt)}] if alt else []
        block: dict[str, object] = {"type": BlockType.IMAGE, "content": ""}
        if resolved.image_base64:
            block["image_base64"] = resolved.image_base64
        if resolved.image_url:
            block["image_url"] = resolved.image_url
        blocks: list[dict[str, object]] = [block]
        annotation = (caption or (alt if emit_alt_caption else "")).strip()
        if annotation:
            blocks.append({"type": BlockType.IMAGE_CAPTION, "content": text_spans(annotation)})
        return blocks

    def _parse_figure(
        self,
        element: etree._Element,
        style: TextStyle,
        visibility_hidden: bool = False,
    ) -> list[dict[str, object]]:
        """Parse visual body, caption and footnote by standard label or complete token."""
        annotations = [
            (child, kind)
            for child in element
            if isinstance(child.tag, str) and (kind := self._visual_annotation_kind(child)) is not None
        ]
        annotation_elements = {child for child, _ in annotations}
        docvortex_figure = "docvortex-figure" in (element.get("class") or "").casefold().split()
        blocks, visual_blocks_by_child = self._parse_figure_contents(
            element,
            style,
            visibility_hidden,
            annotation_elements=annotation_elements,
            emit_alt_caption=not docvortex_figure and not annotations,
        )
        annotation_targets = self._figure_annotation_targets(
            element,
            annotation_elements,
            visual_blocks_by_child,
        )

        annotations_by_visual: dict[int, list[dict[str, object]]] = {}
        unbound_annotations: list[dict[str, object]] = []
        for annotation, kind in annotations:
            resolved = self.stylesheet.resolve(annotation, style, visibility_hidden)
            if resolved.subtree_hidden:
                continue
            target = annotation_targets.get(annotation)
            visual_type = _raw_visual_type(target.get("type")) if target is not None else None
            annotation_type = VISUAL_TYPE_MAPPING[visual_type][kind] if visual_type is not None else BlockType.TEXT
            annotation_blocks: list[dict[str, object]] = []
            for segment in self._render_inline_children_ordered(annotation, resolved.text, resolved.visibility_hidden):
                if isinstance(segment, list):
                    if content := _strip_source_spans(segment):
                        annotation_blocks.append({"type": annotation_type, "content": content})
                    continue
                if visual_type is not None and segment.get("type") == BlockType.TEXT:
                    segment = {**segment, "type": annotation_type}
                annotation_blocks.append(segment)
            if target is not None and visual_type is not None:
                annotations_by_visual.setdefault(id(target), []).extend(annotation_blocks)
            else:
                unbound_annotations.extend(annotation_blocks)

        output: list[dict[str, object]] = []
        for block in blocks:
            output.append(block)
            output.extend(annotations_by_visual.get(id(block), ()))
        output.extend(unbound_annotations)
        return output

    def _parse_figure_contents(
        self,
        element: etree._Element,
        style: TextStyle,
        visibility_hidden: bool,
        *,
        annotation_elements: set[etree._Element],
        emit_alt_caption: bool,
    ) -> tuple[list[dict[str, object]], dict[etree._Element, list[dict[str, object]]]]:
        """Buffer the figure text in the order of DOM, and split the text block before and after visual extras."""
        blocks: list[dict[str, object]] = []
        visual_blocks_by_child: dict[etree._Element, list[dict[str, object]]] = {}
        inline_parts: list[_InlineSpanDict] = _SourceSpans()
        if not visibility_hidden:
            _extend_source_spans(inline_parts, self._render_text(element.text, style))

        def flush_inline() -> None:
            """Write the current continuous text of figure as ordinary text block."""
            content = _strip_source_spans(inline_parts)
            inline_parts.clear()
            if content:
                blocks.append({"type": BlockType.TEXT, "content": content})

        for child in element:
            if not isinstance(child.tag, str):
                if not visibility_hidden:
                    _extend_source_spans(inline_parts, self._render_text(entity_text(child), style))
                    _extend_source_spans(inline_parts, self._render_text(child.tail, style))
                continue
            if child in annotation_elements:
                if not visibility_hidden:
                    _extend_source_spans(inline_parts, self._render_text(child.tail, style))
                continue

            first_child_block = len(blocks)
            name = local_name(child)
            if name in {"img", "image"}:
                flush_inline()
                child_style = self.stylesheet.resolve(child, style, visibility_hidden)
                if not child_style.subtree_hidden and not child_style.visibility_hidden:
                    blocks.extend(self._image_blocks(child, emit_alt_caption=emit_alt_caption))
            elif name in BLOCK_TAGS:
                flush_inline()
                blocks.extend(self._parse_block(child, style, visibility_hidden))
            else:
                for segment in self._render_inline_element_ordered(child, style, visibility_hidden):
                    if isinstance(segment, list):
                        _extend_source_spans(inline_parts, segment)
                    else:
                        flush_inline()
                        blocks.append(segment)
            if not visibility_hidden:
                _extend_source_spans(inline_parts, self._render_text(child.tail, style))
            child_visuals = [block for block in blocks[first_child_block:] if _raw_visual_type(block.get("type")) is not None]
            if child_visuals:
                visual_blocks_by_child[child] = child_visuals
        flush_inline()
        return blocks, visual_blocks_by_child

    @staticmethod
    def _figure_annotation_targets(
        figure: etree._Element,
        annotations: set[etree._Element],
        visual_blocks_by_child: dict[etree._Element, list[dict[str, object]]],
    ) -> dict[etree._Element, dict[str, object] | None]:
        """Bind all annotation with bidirectional linear scan, giving priority to the most recent visual."""
        children = [child for child in figure if isinstance(child.tag, str)]
        targets: dict[etree._Element, dict[str, object] | None] = {}
        previous_visual: dict[str, object] | None = None
        for child in children:
            if visuals := visual_blocks_by_child.get(child):
                previous_visual = visuals[-1]
            if child in annotations:
                targets[child] = previous_visual

        next_visual: dict[str, object] | None = None
        for child in reversed(children):
            if visuals := visual_blocks_by_child.get(child):
                next_visual = visuals[0]
            if child in annotations and targets[child] is None:
                targets[child] = next_visual
        return targets

    def _has_contextual_visual_annotation(self, element: etree._Element) -> bool:
        """Non-standard container resolution is only enabled when direct full token annotation coexists with visual descendants."""
        children = [child for child in element if isinstance(child.tag, str)]
        if not any(self._visual_annotation_kind(child) is not None for child in children):
            return False
        return any(
            local_name(candidate) in _VISUAL_ELEMENT_TAGS
            for child in children
            if self._visual_annotation_kind(child) is None
            for candidate in [child, *child.iterdescendants()]
            if isinstance(candidate.tag, str)
        )

    @staticmethod
    def _visual_annotation_kind(element: etree._Element) -> str | None:
        """Returns the caption/footnote role with the standard tag, role or the full class/id token."""
        if local_name(element) == "figcaption":
            return "caption"
        tokens = _semantic_tokens(element)
        roles = frozenset((element.get("role") or "").casefold().split())
        if tokens & _CAPTION_TOKENS or roles & {"caption", "doc-subtitle"}:
            return "caption"
        if tokens & _FOOTNOTE_TOKENS or roles & {"doc-footnote", "note"}:
            return "footnote"
        return None

    def _parse_svg(
        self,
        element: etree._Element,
        style: TextStyle,
        visibility_hidden: bool = False,
    ) -> list[dict[str, object]]:
        """Best effort extraction of title/desc/text and static image from SVG."""
        blocks: list[dict[str, object]] = []
        texts: list[str] = []

        def visit(parent: etree._Element, inherited: TextStyle, inherited_visibility_hidden: bool) -> None:
            """Access candidate nodes in SVG tree order and allow visible descendants to resume output."""
            for child in parent:
                if not isinstance(child.tag, str):
                    continue
                resolved = self.stylesheet.resolve(child, inherited, inherited_visibility_hidden)
                if resolved.subtree_hidden:
                    continue
                name = local_name(child)
                if name in {"title", "desc", "text"}:
                    value = self._visible_plain_text(child, resolved.text, resolved.visibility_hidden)
                    if value and value not in texts:
                        texts.append(value)
                elif name == "image":
                    if not resolved.visibility_hidden:
                        blocks.extend(self._image_blocks(child))
                else:
                    visit(child, resolved.text, resolved.visibility_hidden)

        visit(element, style, visibility_hidden)
        if texts:
            blocks.insert(0, {"type": BlockType.TEXT, "content": text_spans("\n".join(texts))})
        return blocks

    def _parse_table(
        self,
        table: etree._Element,
        style: TextStyle,
        visibility_hidden: bool = False,
    ) -> list[dict[str, object]]:
        """Rebuild the whitelisted HTML table and project caption as a table description."""
        markup = self._serialize_table_node(table, style, visibility_hidden)
        if not markup:
            return []
        blocks: list[dict[str, object]] = [{"type": BlockType.TABLE, "content": markup}]
        caption_element = next(
            (child for child in table if isinstance(child.tag, str) and local_name(child) == "caption"),
            None,
        )
        if caption_element is not None:
            caption_style = self.stylesheet.resolve(caption_element, style, visibility_hidden)
            if not caption_style.subtree_hidden:
                caption = self._visible_plain_text(caption_element, caption_style.text, caption_style.visibility_hidden)
                if caption:
                    blocks.append({"type": BlockType.TABLE_CAPTION, "content": text_spans(caption)})
        return blocks

    def _serialize_table_node(
        self,
        element: etree._Element,
        inherited: TextStyle,
        inherited_visibility_hidden: bool = False,
        *,
        row_link_target: str | None = None,
    ) -> str:
        """Recursively serialize safe table structures, inline styles, links, formulas, and images."""
        resolved = self.stylesheet.resolve(element, inherited, inherited_visibility_hidden)
        if resolved.subtree_hidden:
            return ""
        name = local_name(element)
        if name == "caption" or name in SKIPPED_TAGS:
            return ""
        allowed = {
            "a",
            "b",
            "br",
            "code",
            "col",
            "colgroup",
            "em",
            "i",
            "img",
            "math",
            "p",
            "s",
            "span",
            "strong",
            "sub",
            "sup",
            "table",
            "tbody",
            "td",
            "tfoot",
            "th",
            "thead",
            "tr",
            "u",
        }
        if name not in allowed:
            return self._serialize_table_children(
                element,
                resolved.text,
                resolved.visibility_hidden,
                row_link_target=row_link_target,
            )
        if name == "br":
            return "" if resolved.visibility_hidden else "<br>"
        if name == "math":
            if resolved.visibility_hidden:
                return ""
            formula = self._formula_extraction(element)
            if formula is not None:
                return f"<eq>{html.escape(formula.latex, quote=False)}</eq>"
            fallback = self._visible_plain_text(element, resolved.text, resolved.visibility_hidden)
            return html.escape(fallback, quote=False)
        if name == "img":
            if resolved.visibility_hidden:
                return ""
            source = element.get("src") or ""
            alt_text = (element.get("alt") or "").strip()
            image = self.context.resolve_image(source, alt=alt_text)
            if image is None:
                return html.escape(alt_text, quote=False)
            image_source = image.image_base64 or image.image_url
            alt = html.escape(image.alt or alt_text, quote=True)
            return f'<img src="{html.escape(image_source, quote=True)}" alt="{alt}">' if image_source else alt
        if name == "tr":
            row_link_target = self._toc_table_row_target(element)
        attributes: list[str] = []
        if name in {"td", "th"}:
            for attribute in ("colspan", "rowspan", "scope"):
                value = (element.get(attribute) or "").strip()
                if attribute == "scope" and value in {"col", "colgroup", "row", "rowgroup"}:
                    attributes.append(f'{attribute}="{value}"')
                elif span := bounded_table_span(value):
                    attributes.append(f'{attribute}="{span}"')
        elif name in {"col", "colgroup"}:
            if span := bounded_table_span((element.get("span") or "").strip()):
                attributes.append(f'span="{span}"')
        if name == "a":
            target = self.context.resolve_link(element.get("href") or "")
            if target:
                attributes.append(f'href="{html.escape(target, quote=True)}"')
        inner = self._serialize_table_children(
            element,
            resolved.text,
            resolved.visibility_hidden,
            row_link_target=row_link_target,
        )
        if resolved.visibility_hidden and not inner:
            return ""
        if name in {"td", "th"} and row_link_target and self._table_cell_can_inherit_toc_link(element):
            inner = f'<a href="{html.escape(row_link_target, quote=True)}">{inner}</a>'
        attrs = f" {' '.join(attributes)}" if attributes else ""
        return f"<{name}{attrs}>{inner}</{name}>"

    def _serialize_table_children(
        self,
        element: etree._Element,
        style: TextStyle,
        visibility_hidden: bool = False,
        *,
        row_link_target: str | None = None,
    ) -> str:
        """Serialize table node's text, child elements, and tail."""
        parts = [] if visibility_hidden else [self._render_table_text(element.text, style)]
        for child in element:
            if isinstance(child.tag, str):
                parts.append(self._serialize_table_node(child, style, visibility_hidden, row_link_target=row_link_target))
            elif not visibility_hidden:
                parts.append(self._render_table_text(entity_text(child), style))
            if not visibility_hidden:
                parts.append(self._render_table_text(child.tail, style))
        return "".join(parts)

    def _toc_table_row_target(self, row: etree._Element) -> str | None:
        """Returns internal links for table of contents table rows that strictly match a single target title."""
        links = [
            element
            for element in row.iter()
            if isinstance(element.tag, str) and local_name(element) == "a" and (element.get("href") or "").strip()
        ]
        if not links:
            return None
        resolved_targets: list[str] = []
        for link in links:
            target = self.context.resolve_link(link.get("href") or "")
            if target is None or not target.startswith("#"):
                return None
            resolved_targets.append(target)
        if len(set(resolved_targets)) != 1:
            return None
        target = resolved_targets[0]
        title = self.context.heading_label(target[1:])
        if title is None:
            return None
        cells = [child for child in row if isinstance(child.tag, str) and local_name(child) in {"td", "th"}]
        row_label = " ".join(value for cell in cells if (value := visible_text(cell)))
        normalized_row = _WHITESPACE_RE.sub(" ", html.unescape(row_label)).strip().casefold()
        normalized_title = _WHITESPACE_RE.sub(" ", html.unescape(title)).strip().casefold()
        return target if normalized_row and normalized_row == normalized_title else None

    @staticmethod
    def _table_cell_can_inherit_toc_link(cell: etree._Element) -> bool:
        """Only plain text and inline style cells are allowed to inherit unique internal links for table of contents rows."""
        if not visible_text(cell):
            return False
        allowed_inline = {"b", "br", "code", "em", "i", "s", "span", "strong", "sub", "sup", "u"}
        return all(isinstance(child.tag, str) and local_name(child) in allowed_inline for child in cell.iterdescendants())

    @staticmethod
    def _render_table_text(value: str | None, style: TextStyle) -> str:
        """Escape table text and wrap it into safe HTML style tags supported by renderer."""
        rendered = html.escape(clean_text_node(value), quote=False)
        if not rendered:
            return ""
        for enabled, tag in (
            (style.bold, "strong"),
            (style.italic, "em"),
            (style.underline, "u"),
            (style.strikethrough, "s"),
            (style.superscript, "sup"),
            (style.subscript, "sub"),
        ):
            if enabled:
                rendered = f"<{tag}>{rendered}</{tag}>"
        return rendered

    def _parse_list(
        self,
        element: etree._Element,
        style: TextStyle,
        visibility_hidden: bool = False,
    ) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
        """Parse ordered/unordered lists and project into continuous Arabic numbering structures."""
        if self._list_contains_page_blocks(element):
            return self._parse_list_with_page_blocks(element, style, visibility_hidden)
        ordered = local_name(element) == "ol"
        items = [child for child in element if isinstance(child.tag, str) and local_name(child) == "li"]
        if not items:
            return None, []
        children: list[dict[str, object]] = []
        extras: list[dict[str, object]] = []
        for item in items:
            item_style = self.stylesheet.resolve(item, style, visibility_hidden)
            if item_style.subtree_hidden:
                continue
            if self.context.note_anchor(item) is not None:
                extras.extend(self._parse_note_element(item, item_style.text, item_style.visibility_hidden))
                continue
            content_parts: list[_InlineSpanDict] = _SourceSpans()
            if not item_style.visibility_hidden:
                _extend_source_spans(content_parts, self._render_text(item.text, item_style.text))
            nested_lists: list[dict[str, object]] = []
            for child in item:
                if not isinstance(child.tag, str):
                    if not item_style.visibility_hidden:
                        _extend_source_spans(content_parts, self._render_text(entity_text(child), item_style.text))
                        _extend_source_spans(content_parts, self._render_text(child.tail, item_style.text))
                    continue
                name = local_name(child)
                if name in {"ul", "ol"}:
                    nested_style = self.stylesheet.resolve(child, item_style.text, item_style.visibility_hidden)
                    if not nested_style.subtree_hidden:
                        nested, nested_extras = self._parse_list(child, nested_style.text, nested_style.visibility_hidden)
                        if nested is not None:
                            nested_lists.append(nested)
                        extras.extend(nested_extras)
                elif name in {"table", "figure", "svg"}:
                    extras.extend(self._parse_block(child, item_style.text, item_style.visibility_hidden))
                elif name in BLOCK_TAGS:
                    child_style = self.stylesheet.resolve(child, item_style.text, item_style.visibility_hidden)
                    if not child_style.subtree_hidden:
                        if self.context.note_anchor(child) is not None:
                            extras.extend(self._parse_note_element(child, child_style.text, child_style.visibility_hidden))
                        else:
                            rendered, child_extras = self._render_inline_children(
                                child,
                                child_style.text,
                                child_style.visibility_hidden,
                            )
                            _append_list_block_content(content_parts, rendered)
                            extras.extend(child_extras)
                else:
                    rendered, child_extras = self._render_inline_element(child, item_style.text, item_style.visibility_hidden)
                    _extend_source_spans(content_parts, rendered)
                    extras.extend(child_extras)
                if not item_style.visibility_hidden:
                    _extend_source_spans(content_parts, self._render_text(child.tail, item_style.text))
            content = _strip_source_spans(content_parts)
            if content:
                children.append({"type": BlockType.TEXT, "content": content})
            children.extend(nested_lists)
        if not children:
            return None, extras
        block: dict[str, object] = {
            "type": BlockType.LIST,
            "attribute": "ordered" if ordered else "unordered",
            "content": children,
        }
        if ordered:
            block["start"] = self._ordered_list_start(element)
        return block, extras

    @staticmethod
    def _list_contains_page_blocks(element: etree._Element) -> bool:
        """Determines whether the list contains visual, code, or formula subtrees that may be promoted to page siblings."""
        return any(
            isinstance(candidate.tag, str) and local_name(candidate) in _LIST_PAGE_BLOCK_TAGS
            for candidate in element.iterdescendants()
        )

    def _parse_list_with_page_blocks(
        self,
        element: etree._Element,
        style: TextStyle,
        visibility_hidden: bool,
    ) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
        """Cut the list containing visual into ordered list/text/page block fragments, keeping the DOM reading order."""
        ordered = local_name(element) == "ol"
        list_start = self._ordered_list_start(element) if ordered else 1
        items = [child for child in element if isinstance(child.tag, str) and local_name(child) == "li"]
        pending_children: list[dict[str, object]] = []
        pending_start = list_start
        output: list[dict[str, object]] = []
        visible_item_ordinal = 0
        has_page_blocks = False

        def flush_pending() -> None:
            """Writes the current consecutive list item as a top-level list block."""
            nonlocal pending_children
            if not pending_children:
                return
            output.append(self._build_raw_list_block(pending_children, ordered=ordered, start=pending_start))
            pending_children = []

        for item in items:
            item_style = self.stylesheet.resolve(item, style, visibility_hidden)
            if item_style.subtree_hidden:
                continue
            if self.context.note_anchor(item) is not None:
                flush_pending()
                output.extend(self._parse_note_element(item, item_style.text, item_style.visibility_hidden))
                has_page_blocks = True
                continue

            segments = self._normalize_list_item_segments(
                self._render_inline_children_ordered(item, item_style.text, item_style.visibility_hidden)
            )
            page_positions = [
                index
                for index, segment in enumerate(segments)
                if not isinstance(segment, list) and segment.get("type") != BlockType.LIST
            ]
            if not page_positions:
                item_children = self._list_item_children(segments)
                if item_children:
                    if not pending_children:
                        pending_start = list_start + visible_item_ordinal
                    pending_children.extend(item_children)
                    visible_item_ordinal += 1
                continue

            first_page_position = page_positions[0]
            prefix_children = self._list_item_children(segments[:first_page_position])
            if not pending_children:
                pending_start = list_start + visible_item_ordinal
            pending_children.extend(prefix_children or [{"type": BlockType.TEXT, "content": []}])
            flush_pending()

            for segment in segments[first_page_position:]:
                if isinstance(segment, list):
                    content = _strip_source_spans(segment)
                    if content:
                        output.append({"type": BlockType.TEXT, "content": content})
                else:
                    output.append(segment)
            visible_item_ordinal += 1
            has_page_blocks = True

        if not has_page_blocks:
            return (
                self._build_raw_list_block(pending_children, ordered=ordered, start=list_start) if pending_children else None,
                [],
            )
        flush_pending()
        return None, output

    @staticmethod
    def _normalize_list_item_segments(
        segments: list[_InlineProjectionSegment],
    ) -> list[_InlineProjectionSegment]:
        """Restore ordinary text block inside the list to text fragments, retaining visual/list page boundaries."""
        normalized: list[_InlineProjectionSegment] = []
        for segment in segments:
            if isinstance(segment, dict) and segment.get("type") == BlockType.TEXT:
                content = segment.get("content")
                _append_inline_segment(normalized, content if isinstance(content, list) else [])
            else:
                _append_inline_segment(normalized, segment)
        return normalized

    @staticmethod
    def _list_item_children(segments: list[_InlineProjectionSegment]) -> list[dict[str, object]]:
        """Convergence list fragments of pageless visual into a text leaf and its nested lists."""
        content: list[_InlineSpanDict] = _SourceSpans()
        for segment in segments:
            if isinstance(segment, list):
                _extend_source_spans(content, segment)
        content = _strip_source_spans(content)
        children = [{"type": BlockType.TEXT, "content": content}] if content else []
        children.extend(segment for segment in segments if isinstance(segment, dict) and segment.get("type") == BlockType.LIST)
        return children

    @staticmethod
    def _build_raw_list_block(
        children: list[dict[str, object]],
        *,
        ordered: bool,
        start: int,
    ) -> dict[str, object]:
        """Construct a raw list block that can be numbered by existing coordinate post-processing."""
        block: dict[str, object] = {
            "type": BlockType.LIST,
            "attribute": "ordered" if ordered else "unordered",
            "content": children,
        }
        if ordered:
            block["start"] = start
        return block

    @staticmethod
    def _ordered_list_start(element: etree._Element) -> int:
        """Read the unique common starting value of the ordered list. Illegal or negative values fall back to one."""
        try:
            start = int(element.get("start") or 1)
        except ValueError:
            return 1
        return start if start >= 0 else 1

    @staticmethod
    def _formula_extraction(element: etree._Element) -> FormulaExtraction | None:
        """Call the shared formula priority and return the bare LaTeX and source information."""
        return extract_formula(element)

    @staticmethod
    def _code_language_hint(element: etree._Element) -> str | None:
        """Extracts the safe language prompt from the standard class or data attribute of pre/code."""
        candidates = [element, *[child for child in element if isinstance(child.tag, str) and local_name(child) == "code"]]
        for candidate in candidates:
            for attribute in ("data-language", "data-lang"):
                value = (candidate.get(attribute) or "").strip()
                if re.fullmatch(r"[A-Za-z0-9_.+#-]+", value):
                    return value
            for token in (candidate.get("class") or "").split():
                normalized = token.casefold()
                for prefix in ("language-", "lang-"):
                    if normalized.startswith(prefix):
                        value = token[len(prefix) :]
                        if re.fullmatch(r"[A-Za-z0-9_.+#-]+", value):
                            return value
        return None


__all__ = [
    "BLOCK_TAGS",
    "MarkupContext",
    "MarkupProjector",
    "ResolvedMarkupImage",
    "SKIPPED_TAGS",
    "bounded_table_span",
    "clean_text_node",
    "entity_text",
    "local_name",
    "visible_raw_text_with_style",
    "visible_text",
]

# Keep the existing public type pickle path, with all old and new entries pointing to the same class.
preserve_type_module(ResolvedMarkupImage, "docvortex.analyzers.native._shared.markup.projector")
preserve_type_module(MarkupContext, "docvortex.analyzers.native._shared.markup.projector")
preserve_type_module(MarkupProjector, "docvortex.analyzers.native._shared.markup.projector")
