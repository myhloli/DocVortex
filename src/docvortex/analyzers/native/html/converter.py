"""Standalone HTML native converter to single page DocVortex raw model-list."""

from __future__ import annotations

import json
import re
from typing import Any, BinaryIO

from loguru import logger

from docvortex.content.markup import MarkupProjector, MarkupStylesheet
from docvortex.document.contracts import HtmlSourceContext

from ....codecs.html import decode_docvortex_html_wire
from ....content.spans import text_spans
from ....schema import BlockType
from .anchors import HtmlAnchorRegistry, append_referenced_notes
from .constants import MAX_HTML_BYTES, MAX_HTML_RENDERED_BYTES
from .document import HtmlDocument, parse_html_document
from .errors import HtmlResourceLimitError
from .resources import HtmlResourceContext
from .selector import select_auto_content


class HtmlConverter:
    """Convert static HTML to a logical page without bbox."""

    def __init__(self) -> None:
        """Initialize empty page results."""
        self.pages: list[list[dict[str, Any]]] = []

    def convert(
        self,
        file_binary: BinaryIO,
        *,
        source_context: HtmlSourceContext | None = None,
    ) -> None:
        """Read the caller's HTML stream, automatically select the text and generate a single page raw blocks."""
        file_bytes = file_binary.read(MAX_HTML_BYTES + 1)
        if len(file_bytes) > MAX_HTML_BYTES:
            raise HtmlResourceLimitError(f"HTML resource limit exceeded: max_html_bytes={MAX_HTML_BYTES}")
        document = parse_html_document(file_bytes, source_context)
        resources = HtmlResourceContext(document.source_context, base_href=document.base_href)
        self._convert_document(document, resources)

    def _convert_document(self, document: HtmlDocument, resources: HtmlResourceContext) -> None:
        """Reuse parsed DOM and resource adapter for HTML to share text projection with web archives."""
        wire_result = decode_docvortex_html_wire(document.body, resources)
        if wire_result.blocks is not None:
            blocks = wire_result.blocks
            log_values = ("docvortex_exact", 1.0, 1.0, "version_1")
        else:
            if wire_result.fallback_reason is not None:
                logger.warning("DocVortex HTML marker fallback reason={}", wire_result.fallback_reason)
            stylesheet = _load_stylesheet(document, resources)
            selection = select_auto_content(document.body, stylesheet)
            selected_root = append_referenced_notes(
                selection.root,
                document.body,
                stylesheet=stylesheet,
                resolve_same_document_fragment=resources.same_document_fragment,
            )
            source_key = document.source_context.source_uri or "html"
            anchors = HtmlAnchorRegistry(selected_root, stylesheet, source_key=source_key)
            resources.bind_anchors(anchors)
            blocks = MarkupProjector(
                selected_root,
                resources,
                stylesheet,
                single_document_title=True,
            ).convert()
            if not any(block.get("type") == BlockType.DOC_TITLE for block in blocks):
                if title := _document_title(document):
                    blocks.insert(0, {"type": BlockType.DOC_TITLE, "level": 1, "content": text_spans(title)})
            log_values = (
                selection.mode_used,
                selection.confidence,
                selection.retained_text_ratio,
                selection.reason,
            )
        rendered_bytes = len(json.dumps(blocks, ensure_ascii=False, separators=(",", ":")).encode())
        if rendered_bytes > MAX_HTML_RENDERED_BYTES:
            raise HtmlResourceLimitError(f"HTML projection exceeds max_html_rendered_bytes={MAX_HTML_RENDERED_BYTES}")
        logger.debug(
            "HTML content selection finished mode={} confidence={:.3f} retained_text_ratio={:.3f} reason={}",
            *log_values,
        )
        self.pages = [blocks]


def _load_stylesheet(document: HtmlDocument, resources: HtmlResourceContext) -> MarkupStylesheet:
    """Load a supported subset of native stylesheet and inline style in head document order."""
    stylesheet = MarkupStylesheet()
    for source in document.stylesheets:
        if source.kind == "inline":
            resources.charge_inline_stylesheet(source.value)
            stylesheet.add(source.value)
        elif css := resources.load_stylesheet(source.value):
            stylesheet.add(css)
    return stylesheet


def _document_title(document: HtmlDocument) -> str | None:
    """Press OpenGraph/title priority to return the title of deduplication and conservative desite suffix."""
    title = (document.open_graph_title or document.title or "").strip()
    if not title:
        return None
    site_name = (document.site_name or "").strip()
    if site_name:
        for separator in (" - ", " | ", " · ", " — ", " _ "):
            suffix = f"{separator}{site_name}"
            prefix = f"{site_name}{separator}"
            if title.casefold().endswith(suffix.casefold()):
                title = title[: -len(suffix)].strip()
                break
            if title.casefold().startswith(prefix.casefold()):
                title = title[len(prefix) :].strip()
                break
    return re.sub(r"\s+", " ", title) or None


__all__ = ["HtmlConverter"]
