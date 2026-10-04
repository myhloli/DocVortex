"""Convert EPUB XHTML/SVG content document to DocVortex raw blocks."""

from __future__ import annotations

import base64
from dataclasses import dataclass

from lxml import etree  # type: ignore[reportMissingImports]
from lxml import html as lxml_html

from docvortex.content.markup import (
    MarkupAnchorDocument,
    MarkupAnchorRegistry,
    MarkupProjector,
    MarkupStylesheet,
    ResolvedMarkupImage,
    element_id,
    visible_element_text,
)
from docvortex.content.markup.projector import (
    BLOCK_TAGS as _BLOCK_TAGS,
)
from docvortex.content.markup.projector import (
    SKIPPED_TAGS as _SKIPPED_TAGS,
)
from docvortex.content.markup.projector import (
    clean_text_node as _clean_text_node,
)
from docvortex.content.markup.projector import (
    entity_text as _entity_text,
)
from docvortex.content.markup.projector import (
    local_name as _local_name,
)

from ....content.spans import normalize_span_dicts
from ....foundation._image_payload import parse_image_data_uri_strict
from ....foundation._svg_raster import serialize_svg_image
from .._shared.hyperlink import sanitize_hyperlink_target
from .constants import IMAGE_MEDIA_BY_EXTENSION, SVG_MEDIA_TYPE
from .package import EpubPackage

_INDIVIDUAL_NOTE_TYPE_ORDER = ("footnote", "endnote", "rearnote")
_INDIVIDUAL_NOTE_ROLE_ORDER = ("doc-footnote", "doc-endnote")
_INDIVIDUAL_NOTE_TYPES = frozenset(_INDIVIDUAL_NOTE_TYPE_ORDER)
_INDIVIDUAL_NOTE_ROLES = frozenset(_INDIVIDUAL_NOTE_ROLE_ORDER)
_NOTE_BLOCK_TAGS = _BLOCK_TAGS | {"li"}
_NOTE_NON_TEXT_SUBTREES = frozenset(
    {
        "figure",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "math",
        "ol",
        "pre",
        "svg",
        "table",
        "ul",
    }
)


def _unwrap_epub_type_quotes(value: str) -> str:
    """Remove single layer of paired peripheral quotes to avoid treating multiple individually quoted tokens as one."""
    normalized = value.strip()
    quote_pairs = {'"': '"', "'": "'", "“": "”", "‘": "’"}
    if len(normalized) < 2 or quote_pairs.get(normalized[0]) != normalized[-1]:
        return normalized
    inner = normalized[1:-1]
    if normalized[0] in inner or normalized[-1] in inner:
        return normalized
    return inner.strip()


def _epub_types(element: etree._Element) -> frozenset[str]:
    """Read the structural semantics token in the EPUB namespace or unnamed type attribute."""
    values: list[str] = []
    for name, value in element.attrib.items():
        local_name = etree.QName(name).localname if name.startswith("{") else name.split(":", 1)[-1]
        if local_name == "type":
            values.extend(_unwrap_epub_type_quotes(token) for token in _unwrap_epub_type_quotes(value).casefold().split())
    return frozenset(values)


def _roles(element: etree._Element) -> frozenset[str]:
    """Read lowercase semantics token in the ARIA role attribute."""
    return frozenset((element.get("role") or "").casefold().split())


def _is_individual_note(element: etree._Element) -> bool:
    """Determine whether the block-level element represents a single EPUB Footnote/Endnote."""
    if _local_name(element) not in _NOTE_BLOCK_TAGS:
        return False
    return bool(_epub_types(element) & _INDIVIDUAL_NOTE_TYPES or _roles(element) & _INDIVIDUAL_NOTE_ROLES)


def _note_semantic(element: etree._Element) -> str:
    """Return EPUB type or ARIA role of note in fixed priority order."""
    epub_types = _epub_types(element)
    for note_type in _INDIVIDUAL_NOTE_TYPE_ORDER:
        if note_type in epub_types:
            return note_type
    roles = _roles(element)
    for role in _INDIVIDUAL_NOTE_ROLE_ORDER:
        if role in roles:
            return role
    return "note"


def _note_has_text_block(element: etree._Element) -> bool:
    """Determine whether note can generate non-empty text, thereby avoiding the registration of anchor without text objects."""
    if _clean_text_node(element.text).strip():
        return True
    for child in element:
        if child.tail and _clean_text_node(child.tail).strip():
            return True
        if not isinstance(child.tag, str):
            if _entity_text(child):
                return True
            continue
        name = _local_name(child)
        if name in _SKIPPED_TAGS or name in _NOTE_NON_TEXT_SUBTREES or name in {"img", "image"}:
            continue
        if _note_has_text_block(child):
            return True
    return False


def _load_chapter_stylesheet(package: EpubPackage, chapter_path: str, root: etree._Element) -> MarkupStylesheet:
    """Load CSS and inline style in the package in order of chapter head."""
    stylesheet = MarkupStylesheet()
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        name = _local_name(element)
        if name == "link" and "stylesheet" in (element.get("rel") or "").casefold().split():
            target = package.resolve_reference(element.get("href") or "", base_part=chapter_path)
            if target is None:
                continue
            data = package.read_part(target.path)
            if data is not None:
                stylesheet.add(data.decode("utf-8-sig", errors="replace"))
        elif name == "style":
            stylesheet.add("".join(element.itertext()))
    return stylesheet


class _EpubAnchorPolicy:
    """Maintain the existing contract between EPUB title, footnote identity and feasibility judgment."""

    anchor_prefix = "epub"
    register_document_start = True

    @staticmethod
    def heading_identity(element: etree._Element, ordinal: int) -> str:
        """Generate EPUB title identity according to source ID or anonymous title serial number."""
        return f"{element_id(element) or 'heading'}-{ordinal}"

    @staticmethod
    def is_materializable_note(element: etree._Element, document: MarkupAnchorDocument) -> bool:
        """Follow EPUB note semantics, text block capabilities and final visibility judgment."""
        return _is_individual_note(element) and _note_has_text_block(element) and bool(visible_element_text(element, document))

    @staticmethod
    def note_identity(element: etree._Element, ordinal: int) -> str:
        """Generate EPUB footnote identity according to note type, source ID and serial number in chapter."""
        source_id = element_id(element)
        return f"note-{_note_semantic(element)}-{source_id or 'anonymous'}-{ordinal}"


class EpubAnchorRegistry:
    """Create an alias table of chapter path, text, title and note fragment to actual canonical anchor."""

    def __init__(self, chapters: list[tuple[str, etree._Element]], package: EpubPackage) -> None:
        """Pre-scan all selected XHTML chapters, and establish mapping between title, note and chapter starting point."""
        self._package = package
        self._pending_links: dict[str, tuple[str, str | None]] = {}
        documents = [
            MarkupAnchorDocument(
                key=chapter_path,
                root=root,
                stylesheet=_load_chapter_stylesheet(package, chapter_path, root),
                visibility_scope="nearest_body",
                text_normalization="xhtml_whitespace",
            )
            for chapter_path, root in chapters
        ]
        self._registry = MarkupAnchorRegistry(documents, _EpubAnchorPolicy())

    def register_text_targets(
        self,
        chapter_path: str,
        root: etree._Element,
        sources: list[tuple[etree._Element, dict[str, object]]],
    ) -> None:
        """After the chapter projection is completed, the real text object is registered, and the existing title and footnote identities are not modified."""
        self._registry.register_text_targets(chapter_path, root, sources)

    def defer_link(self, href: str, *, base_part: str) -> str | None:
        """The targets in the package that have not yet been materialized are temporarily stored, and will be uniformly redeemed or downgraded after the cross-chapter projection is completed."""
        normalized = sanitize_hyperlink_target(href, allowed_schemes=(), allow_relative=True, allow_fragment=True)
        target = self._package.resolve_reference(normalized, base_part=base_part) if normalized is not None else None
        if target is None:
            return None
        placeholder = f"#epub-pending-{len(self._pending_links)}"
        self._pending_links[placeholder] = (target.path, target.fragment)
        return placeholder

    def finalize_links(self, pages: list[list[dict[str, object]]]) -> None:
        """Eliminate all parsing period link placeholders and retain the text and style of invalid links."""
        targets = {
            placeholder: (f"#{anchor}" if (anchor := self._registry.resolve_target(path, fragment)) else None)
            for placeholder, (path, fragment) in self._pending_links.items()
        }
        for page in pages:
            _finalize_pending_content(page, targets)
        self._pending_links.clear()

    def heading_anchor(self, heading: etree._Element) -> str | None:
        """Returns a canonical anchor that has the prescanned EPUB header."""
        return self._registry.heading_anchor(heading)

    def heading_label(self, anchor: str) -> str | None:
        """Return the visible label corresponding to the specification EPUB title anchor."""
        return self._registry.heading_label(anchor)

    def note_anchor(self, note: etree._Element) -> str | None:
        """Returns a pre-scanned EPUB Footnote/Endnote anchor."""
        return self._registry.note_anchor(note)

    def resolve_anchor(self, href: str, *, base_part: str) -> str | None:
        """Parse in-package links pointing to registered text, titles, or note, returning anchor without the pound sign."""
        normalized = sanitize_hyperlink_target(
            href,
            allowed_schemes=(),
            allow_relative=True,
            allow_fragment=True,
        )
        if normalized is None:
            return None
        target = self._package.resolve_reference(normalized, base_part=base_part)
        if target is None:
            return None
        return self._registry.resolve_target(target.path, target.fragment)

    def resolve_link(self, href: str, *, base_part: str) -> str | None:
        """Resolving safe external links or EPUB internal links to registered output blocks."""
        external = sanitize_hyperlink_target(href)
        if external is not None:
            return external
        anchor = self.resolve_anchor(href, base_part=base_part)
        return f"#{anchor}" if anchor else None


def build_anchor_registry(chapters: list[tuple[str, etree._Element]], package: EpubPackage) -> EpubAnchorRegistry:
    """Create a cross-chapter anchor registry from parsed chapter tuples."""
    return EpubAnchorRegistry(chapters, package)


@dataclass(frozen=True, slots=True)
class _EpubMarkupContext:
    """Adapt EPUB package resources and document-level anchor to shared markup projector."""

    package: EpubPackage
    chapter_path: str
    anchors: EpubAnchorRegistry

    def resolve_link(self, href: str) -> str | None:
        """Resolve secure external links or anchor within the actual existing EPUB package."""
        return self.anchors.resolve_link(href, base_part=self.chapter_path) or self.anchors.defer_link(
            href, base_part=self.chapter_path
        )

    def resolve_image(self, source: str, *, alt: str = "") -> ResolvedMarkupImage | None:
        """Read and strictly verify the picture reference in a EPUB package, and SVG is rasterized into PNG."""
        target = self.package.resolve_reference(source, base_part=self.chapter_path)
        if target is None:
            return ResolvedMarkupImage(alt=alt) if alt else None
        media_type = (self.package.content_type_for(target.path) or "").casefold()
        extension = target.path.rsplit(".", 1)[-1].casefold() if "." in target.path else ""
        media_type = media_type or IMAGE_MEDIA_BY_EXTENSION.get(extension, "")
        if media_type == SVG_MEDIA_TYPE:
            payload = self.package.read_part(target.path, asset=True)
            if payload is None:
                return ResolvedMarkupImage(alt=alt) if alt else None
            data_uri = serialize_svg_image(payload)
            if data_uri is None:
                return ResolvedMarkupImage(alt=alt) if alt else None
            return ResolvedMarkupImage(image_base64=data_uri, alt=alt)
        if not media_type.startswith("image/"):
            return ResolvedMarkupImage(alt=alt) if alt else None
        payload = self.package.read_part(target.path, asset=True)
        if payload is None:
            return ResolvedMarkupImage(alt=alt) if alt else None
        data_uri = f"data:{media_type};base64,{base64.b64encode(payload).decode('ascii')}"
        try:
            parse_image_data_uri_strict(data_uri)
        except ValueError:
            return ResolvedMarkupImage(alt=alt) if alt else None
        return ResolvedMarkupImage(image_base64=data_uri, alt=alt)

    def heading_anchor(self, heading: etree._Element) -> str | None:
        """Returns a canonical anchor that has the prescanned EPUB header."""
        return self.anchors.heading_anchor(heading)

    def heading_label(self, anchor: str) -> str | None:
        """Return the visible label corresponding to the specification EPUB title anchor."""
        return self.anchors.heading_label(anchor)

    def note_anchor(self, note: etree._Element) -> str | None:
        """Returns a pre-scanned EPUB Footnote/Endnote anchor."""
        return self.anchors.note_anchor(note)


class EpubChapterConverter:
    """Project a XHTML spine item to raw blocks by sharing projector."""

    def __init__(
        self,
        package: EpubPackage,
        chapter_path: str,
        root: etree._Element,
        anchors: EpubAnchorRegistry,
    ) -> None:
        """Bind a single chapter's package, path, DOM and document-level anchor registry."""
        self.package = package
        self.chapter_path = chapter_path
        self.root = root
        self.anchors = anchors
        self.stylesheet = _load_chapter_stylesheet(package, chapter_path, root)

    def convert(self) -> list[dict[str, object]]:
        """Parse XHTML body, and maintain the existing EPUB title and footnote semantics."""
        body = next(
            (element for element in self.root.iter() if isinstance(element.tag, str) and _local_name(element) == "body"),
            None,
        )
        if body is None:
            return []
        context = _EpubMarkupContext(self.package, self.chapter_path, self.anchors)
        projector = MarkupProjector(
            body,
            context,
            self.stylesheet,
            single_document_title=False,
            track_text_sources=True,
        )
        blocks = projector.convert()
        self.anchors.register_text_targets(self.chapter_path, self.root, projector.text_sources)
        return blocks


def _finalize_pending_content(items: list[dict[str, object]], targets: dict[str, str | None]) -> None:
    """Recursively process raw block and inline Span, table HTML synchronously replace or unpack failed links."""
    output: list[dict[str, object]] = []
    flattened = False
    for item in items:
        content = item.get("content")
        if isinstance(content, list):
            _finalize_pending_content(content, targets)
        elif item.get("type") == "table" and isinstance(content, str) and "#epub-pending-" in content:
            root = lxml_html.fragment_fromstring(content, create_parent="div")
            for link in root.iter("a"):
                href = link.get("href")
                if href in targets:
                    if target := targets[href]:
                        link.set("href", target)
                    else:
                        link.drop_tag()
            item["content"] = (root.text or "") + "".join(lxml_html.tostring(child, encoding="unicode") for child in root)
        url = item.get("url")
        if item.get("type") == "hyperlink" and isinstance(url, str) and url in targets:
            if target := targets[url]:
                item["url"] = target
            else:
                if isinstance(content, list):
                    output.extend(content)
                flattened = True
                continue
        output.append(item)
    if flattened:
        # Only the Span list with unpacked invalid links needs to be re-merged to avoid repeated verification of the entire text.
        output = normalize_span_dicts(output)
    items[:] = output


def convert_svg_spine(
    package: EpubPackage,
    chapter_path: str,
    root: etree._Element,
) -> list[dict[str, object]]:
    """Convert standalone SVG spine item to text and raster images in the package."""
    empty_registry = EpubAnchorRegistry([], package)
    context = _EpubMarkupContext(package, chapter_path, empty_registry)
    stylesheet = _load_chapter_stylesheet(package, chapter_path, root)
    return MarkupProjector(root, context, stylesheet).convert_svg()


__all__ = [
    "EpubAnchorRegistry",
    "EpubChapterConverter",
    "build_anchor_registry",
    "convert_svg_spine",
]
