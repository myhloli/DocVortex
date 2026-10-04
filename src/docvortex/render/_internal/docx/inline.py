"""Middle JSON 2.0 Inline writing of Span to Word run, hyperlinks, bookmarks and OMML."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha1
import re

from docx.opc.constants import RELATIONSHIP_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from docx.shared import Pt, RGBColor
from lxml import etree
from loguru import logger

from ....content.inline import inline_plain_text, join_inline_spans
from ....schema import CodeInlineSpan, EquationInlineSpan, HyperlinkSpan, InlineSpan, TextSpan
from .math import DocxFormulaError, latex_to_omml

_BOOKMARK_SAFE_RE = re.compile(r"[^A-Za-z0-9_]+")
_BOOKMARK_MAX_LENGTH = 40
_INVALID_XML_TEXT_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_BARE_SCRIPT_RE = re.compile(r"^\s*(?P<marker>[\^_])\s*\{(?P<content>[^{}]+)\}\s*$")
_VISIBLE_SPACE_STYLES = frozenset({"underline", "strikethrough", "emphasis"})
_NONBREAKING_SPACE = "\u00a0"


@dataclass(frozen=True, slots=True)
class InlineRenderContext:
    """Saves the block location and bookmark table required for inline rendering alerts."""

    bookmarks: BookmarkRegistry
    page_idx: int
    block_index: int | None
    block_type: str

    def location(self) -> str:
        """Returns stable, readable page/block anchor text."""
        return f"page_idx={self.page_idx}, block_index={self.block_index}, block_type={self.block_type}"


class BookmarkRegistry:
    """Map MiddleJson anchor to the legal and unique Word bookmark name."""

    def __init__(self, anchors: Iterable[str]) -> None:
        """Pre-register all anchor to ensure that title, table of contents and footnote forward references are resolved in advance."""
        self._names: dict[str, str] = {}
        self._attached: set[str] = set()
        self._used_names: set[str] = set()
        self._next_id = 0
        for anchor in anchors:
            normalized = anchor.strip()
            if normalized and normalized not in self._names:
                self._names[normalized] = self._allocate_name(normalized)

    def _allocate_name(self, anchor: str) -> str:
        """Assigns a deterministic name to a raw anchor that satisfies the restrictions of Word."""
        base = _BOOKMARK_SAFE_RE.sub("_", anchor).strip("_")
        if not base or not base[0].isalpha():
            base = f"b_{base}"
        candidate = base[:_BOOKMARK_MAX_LENGTH]
        if candidate not in self._used_names:
            self._used_names.add(candidate)
            return candidate

        digest = sha1(anchor.encode("utf-8")).hexdigest()[:8]
        prefix_length = _BOOKMARK_MAX_LENGTH - len(digest) - 1
        candidate = f"{base[:prefix_length]}_{digest}"
        ordinal = 1
        while candidate in self._used_names:
            suffix = f"_{ordinal}"
            candidate = f"{base[: _BOOKMARK_MAX_LENGTH - len(suffix)]}{suffix}"
            ordinal += 1
        self._used_names.add(candidate)
        return candidate

    def resolve(self, anchor: str | None) -> str | None:
        """Resolving registered anchor; null or unknown anchor returns None."""
        if not anchor:
            return None
        return self._names.get(anchor.strip())

    def attach(self, paragraph: Paragraph, anchor: str | None) -> bool:
        """Surround the current paragraph content with anchor bookmark; repeat the text anchor and keep only the first one."""
        normalized = (anchor or "").strip()
        name = self.resolve(normalized)
        if name is None:
            return False
        if normalized in self._attached:
            logger.warning("Duplicate DOCX bookmark anchor ignored: {}", normalized)
            return False

        bookmark_id = str(self._next_id)
        self._next_id += 1
        start = OxmlElement("w:bookmarkStart")
        start.set(qn("w:id"), bookmark_id)
        start.set(qn("w:name"), name)
        end = OxmlElement("w:bookmarkEnd")
        end.set(qn("w:id"), bookmark_id)

        paragraph_xml = paragraph._p
        insert_at = 1 if paragraph_xml.pPr is not None else 0
        paragraph_xml.insert(insert_at, start)
        paragraph_xml.append(end)
        self._attached.add(normalized)
        return True


def append_inline_content(
    paragraph: Paragraph,
    content: list[InlineSpan],
    *,
    context: InlineRenderContext,
) -> None:
    """Appends the MiddleJson inline Span paragraph to the Word paragraph."""
    append_inline_spans(paragraph, content, context=context)


def append_joined_inline_contents(
    paragraph: Paragraph,
    contents: list[list[InlineSpan]],
    *,
    context: InlineRenderContext,
) -> None:
    """Merge continuation segments according to shared boundary rules and write structured inline Span to Word."""
    append_inline_spans(paragraph, join_inline_spans(contents), context=context)


def append_inline_spans(
    paragraph: Paragraph,
    spans: list[InlineSpan],
    *,
    context: InlineRenderContext,
    inherited_styles: tuple[str, ...] = (),
) -> None:
    """Write Span inline to a paragraph while preserving style, link, and formula semantics."""
    _append_spans_to_container(
        paragraph._p,
        paragraph,
        spans,
        context=context,
        inherited_styles=inherited_styles,
        hyperlink=False,
    )


def append_internal_link(
    paragraph: Paragraph,
    label_spans: list[InlineSpan],
    *,
    anchor: str | None,
    context: InlineRenderContext,
) -> bool:
    """Write directory tags as internal links; fall back to normal inline content when the target is missing."""
    bookmark_name = context.bookmarks.resolve(anchor)
    if bookmark_name is None:
        append_inline_spans(paragraph, label_spans, context=context)
        if anchor:
            logger.warning("Unmatched DOCX index anchor: {} ({})", anchor, context.location())
        return False

    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("w:anchor"), bookmark_name)
    hyperlink.set(qn("w:history"), "1")
    paragraph._p.append(hyperlink)
    _append_spans_to_container(
        hyperlink,
        paragraph,
        label_spans,
        context=context,
        inherited_styles=(),
        hyperlink=True,
    )
    return True


def _append_spans_to_container(
    container: etree._Element,
    paragraph: Paragraph,
    spans: list[InlineSpan],
    *,
    context: InlineRenderContext,
    inherited_styles: tuple[str, ...],
    hyperlink: bool,
) -> None:
    """Recursively writes to a paragraph or hyperlink XML container."""
    for span in spans:
        if isinstance(span, TextSpan):
            styles = tuple(dict.fromkeys((*inherited_styles, *span.styles)))
            _append_text_run(
                container,
                paragraph,
                span.content,
                styles=styles,
                hyperlink=hyperlink,
                context=context,
            )
            continue
        if isinstance(span, CodeInlineSpan):
            run = _append_text_run(
                container,
                paragraph,
                span.content,
                styles=inherited_styles,
                hyperlink=hyperlink,
                context=context,
            )
            run.font.name = "Courier New"
            run.font.size = Pt(9)
            continue
        if isinstance(span, EquationInlineSpan):
            bare_script = _BARE_SCRIPT_RE.fullmatch(span.content)
            if bare_script is not None:
                script_style = "superscript" if bare_script.group("marker") == "^" else "subscript"
                styles = tuple(dict.fromkeys((*inherited_styles, script_style)))
                _append_text_run(
                    container,
                    paragraph,
                    bare_script.group("content"),
                    styles=styles,
                    hyperlink=hyperlink,
                    context=context,
                )
                continue
            if hyperlink:
                _append_text_run(
                    container,
                    paragraph,
                    span.content,
                    styles=inherited_styles,
                    hyperlink=True,
                    context=context,
                )
                continue
            try:
                container.append(latex_to_omml(span.content, display=False))
            except DocxFormulaError as exc:
                logger.warning("DOCX inline formula fallback: {} ({})", exc, context.location())
                _append_text_run(
                    container,
                    paragraph,
                    span.content,
                    styles=inherited_styles,
                    hyperlink=False,
                    formula_fallback=True,
                    context=context,
                )
            continue
        if isinstance(span, HyperlinkSpan):
            if hyperlink or not span.url or span.url == ".":
                _append_spans_to_container(
                    container,
                    paragraph,
                    list(span.content),
                    context=context,
                    inherited_styles=inherited_styles,
                    hyperlink=hyperlink,
                )
                continue
            if span.url.startswith("#"):
                _append_inline_internal_link(
                    container,
                    paragraph,
                    span,
                    context=context,
                    inherited_styles=inherited_styles,
                )
                continue
            _append_external_link(
                paragraph,
                span,
                context=context,
                inherited_styles=inherited_styles,
            )
            continue
        raise TypeError(f"Unsupported inline span: {type(span).__name__}")


def _append_inline_internal_link(
    container: etree._Element,
    paragraph: Paragraph,
    span: HyperlinkSpan,
    *,
    context: InlineRenderContext,
    inherited_styles: tuple[str, ...],
) -> None:
    """Write the #anchor inline link as Word bookmark to jump, and the unknown target is reduced to ordinary text."""
    anchor = span.url[1:].strip()
    bookmark_name = context.bookmarks.resolve(anchor)
    if bookmark_name is None:
        _append_spans_to_container(
            container,
            paragraph,
            list(span.content),
            context=context,
            inherited_styles=inherited_styles,
            hyperlink=False,
        )
        return

    internal_link = OxmlElement("w:hyperlink")
    internal_link.set(qn("w:anchor"), bookmark_name)
    internal_link.set(qn("w:history"), "1")
    container.append(internal_link)
    _append_spans_to_container(
        internal_link,
        paragraph,
        list(span.content),
        context=context,
        inherited_styles=inherited_styles,
        hyperlink=True,
    )


def _append_external_link(
    paragraph: Paragraph,
    span: HyperlinkSpan,
    *,
    context: InlineRenderContext,
    inherited_styles: tuple[str, ...],
) -> None:
    """Create external hyperlink relationship and write the complete tag content."""
    relation_id = paragraph.part.relate_to(
        sanitize_xml_text(span.url, context=context),
        RELATIONSHIP_TYPE.HYPERLINK,
        is_external=True,
    )
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), relation_id)
    paragraph._p.append(hyperlink)
    _append_spans_to_container(
        hyperlink,
        paragraph,
        list(span.content),
        context=context,
        inherited_styles=inherited_styles,
        hyperlink=True,
    )


def _append_text_run(
    container: etree._Element,
    paragraph: Paragraph,
    content: str,
    *,
    styles: tuple[str, ...],
    hyperlink: bool,
    context: InlineRenderContext,
    formula_fallback: bool = False,
) -> Run:
    """Appends a run to the XML container and applies the MiddleJson inline style."""
    run_xml = OxmlElement("w:r")
    container.append(run_xml)
    run = Run(run_xml, paragraph)
    sanitized = sanitize_xml_text(content, context=context)
    run.text = _make_visible_style_spaces(sanitized, styles)
    _apply_run_styles(run, styles)
    if hyperlink:
        run.font.color.rgb = _rgb_color("0563C1")
        run.underline = True
    if formula_fallback:
        run.font.name = "Courier New"
        run.font.size = Pt(9)
        run.font.color.rgb = _rgb_color("555555")
    return run


def _make_visible_style_spaces(content: str, styles: tuple[str, ...]) -> str:
    """Convert the border ASCII spaces of the visible style run to NBSP to avoid hiding the decorative lines of Word."""
    if not content or not _VISIBLE_SPACE_STYLES.intersection(styles):
        return content

    leading_count = len(content) - len(content.lstrip(" "))
    trailing_count = len(content) - len(content.rstrip(" "))
    if leading_count == 0 and trailing_count == 0:
        return content
    if leading_count + trailing_count >= len(content):
        return _NONBREAKING_SPACE * len(content)

    content_end = len(content) - trailing_count if trailing_count else len(content)
    return _NONBREAKING_SPACE * leading_count + content[leading_count:content_end] + _NONBREAKING_SPACE * trailing_count


def sanitize_xml_text(content: str, *, context: InlineRenderContext) -> str:
    """Replace XML 1.0 forbidden characters with U+FFFD and log the locateable renderer alarm."""
    sanitized, replacement_count = _INVALID_XML_TEXT_RE.subn("\ufffd", content)
    if replacement_count:
        logger.warning(
            "DOCX replaced {} XML-incompatible character(s) with U+FFFD ({})",
            replacement_count,
            context.location(),
        )
    return sanitized


def _apply_run_styles(run: Run, styles: tuple[str, ...]) -> None:
    """Apply parsed inline styles to Word run."""
    style_set = set(styles)
    run.bold = "bold" in style_set
    run.italic = "italic" in style_set
    run.underline = "underline" in style_set
    run.font.strike = "strikethrough" in style_set
    run.font.superscript = "superscript" in style_set
    run.font.subscript = "subscript" in style_set and "superscript" not in style_set
    if "emphasis" in style_set:
        run_properties = run._element.get_or_add_rPr()
        emphasis = OxmlElement("w:em")
        emphasis.set(qn("w:val"), "underDot")
        run_properties.append(emphasis)


def _rgb_color(value: str) -> RGBColor:
    """Lazy construction of RGBColor to avoid loading extra objects during module constant phase."""
    return RGBColor.from_string(value)


def plain_inline_text(content: list[InlineSpan]) -> str:
    """Extract the visible text of a piece of content in line MiddleJson."""
    return inline_plain_text(content)


__all__ = [
    "BookmarkRegistry",
    "InlineRenderContext",
    "append_inline_content",
    "append_inline_spans",
    "append_internal_link",
    "append_joined_inline_contents",
    "plain_inline_text",
    "sanitize_xml_text",
]
