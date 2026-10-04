"""Centrally create static HTML/XHTML titles, footnotes and fragment anchor indexes."""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

from lxml import etree  # type: ignore[reportMissingImports]

from docvortex.content.markup.projector import local_name, visible_raw_text_with_style
from docvortex.content.markup.styles import MarkupStylesheet, TextStyle

from ...foundation.type_identity import preserve_type_module

AnchorVisibilityScope: TypeAlias = Literal["all_ancestors", "nearest_body"]
AnchorTextNormalization: TypeAlias = Literal["unicode_whitespace", "xhtml_whitespace"]

_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_XHTML_WHITESPACE_RE = re.compile(r"[\t\r\n\f ]+")
_XML_ID = "{http://www.w3.org/XML/1998/namespace}id"


@dataclass(frozen=True, slots=True)
class MarkupAnchorDocument:
    """Describes the DOM, stylesheet, and compatible visibility rules for a anchor index to be created."""

    key: str
    root: etree._Element
    stylesheet: MarkupStylesheet
    visibility_scope: AnchorVisibilityScope = "all_ancestors"
    text_normalization: AnchorTextNormalization = "unicode_whitespace"


class MarkupAnchorPolicy(Protocol):
    """Defines the stable strategy required by the format adapter to generate headers, footers anchor."""

    anchor_prefix: str
    register_document_start: bool

    def heading_identity(self, element: etree._Element, ordinal: int) -> str:
        """Returns the format of the current title participating in the stable summary identity."""

    def is_materializable_note(self, element: etree._Element, document: MarkupAnchorDocument) -> bool:
        """Determines whether the current element is a format-specific footnote capable of honoring the text anchor."""

    def note_identity(self, element: etree._Element, ordinal: int) -> str:
        """Returns the format of the current footnote participation stable summary identity."""


def element_id(element: etree._Element) -> str | None:
    """Returns HTML id or xml:id with the leading and trailing blanks removed."""
    value = (element.get("id") or element.get(_XML_ID) or "").strip()
    return value or None


def visible_element_text(element: etree._Element, document: MarkupAnchorDocument) -> str:
    """Parses the ancestor style chain according to the document-compatible configuration and returns the final printable plain text."""
    inherited = TextStyle()
    visibility_hidden = False
    chain = [ancestor for ancestor in reversed(list(element.iterancestors())) if isinstance(ancestor.tag, str)]
    if document.visibility_scope == "nearest_body":
        body_index = next((index for index, ancestor in enumerate(chain) if local_name(ancestor) == "body"), None)
        if body_index is not None:
            chain = chain[body_index:]
    chain.append(element)
    for current in chain:
        resolved = document.stylesheet.resolve(current, inherited, visibility_hidden)
        if resolved.subtree_hidden:
            return ""
        inherited = resolved.text
        visibility_hidden = resolved.visibility_hidden
    value = visible_raw_text_with_style(element, document.stylesheet, inherited, visibility_hidden)
    if document.text_normalization == "xhtml_whitespace":
        return _XHTML_WHITESPACE_RE.sub(" ", html.unescape(value)).strip()
    return " ".join(value.split())


def canonical_anchor(prefix: str, document_key: str, identity: str) -> str:
    """Generates a stable twenty-digit summary of anchor by format prefix, document key and identity."""
    digest = hashlib.sha256(f"{document_key}#{identity}".encode()).hexdigest()[:20]
    return f"{prefix}-{digest}"


class MarkupAnchorRegistry:
    """Unified registration of multiple document titles, footnotes, and mapping of source fragment to actual output anchor."""

    def __init__(self, documents: list[MarkupAnchorDocument], policy: MarkupAnchorPolicy) -> None:
        """Stable indexing in caller document order, preserving format-specific identity rules."""
        self._policy = policy
        self._heading_anchors: dict[etree._Element, str] = {}
        self._note_anchors: dict[etree._Element, str] = {}
        self._targets: dict[tuple[str, str | None], str] = {}
        self._heading_labels: dict[str, str] = {}
        for document in documents:
            self._register_document(document)

    def _register_document(self, document: MarkupAnchorDocument) -> None:
        """Registers the title, footer, and all parsable fragment aliases for a single DOM."""
        headings: list[tuple[etree._Element, str]] = []
        for element in document.root.iter():
            if not isinstance(element.tag, str) or local_name(element) not in _HEADING_TAGS:
                continue
            if label := visible_element_text(element, document):
                headings.append((element, label))
        for ordinal, (heading, label) in enumerate(headings):
            identity = self._policy.heading_identity(heading, ordinal)
            anchor = canonical_anchor(self._policy.anchor_prefix, document.key, identity)
            self._heading_anchors[heading] = anchor
            self._heading_labels[anchor] = label

        notes = [
            element
            for element in document.root.iter()
            if isinstance(element.tag, str) and self._policy.is_materializable_note(element, document)
        ]
        for ordinal, note in enumerate(notes):
            identity = self._policy.note_identity(note, ordinal)
            self._note_anchors[note] = canonical_anchor(self._policy.anchor_prefix, document.key, identity)

        if self._policy.register_document_start and headings:
            self._targets[(document.key, None)] = self._heading_anchors[headings[0][0]]
        for element in document.root.iter():
            if not isinstance(element.tag, str) or not (fragment := element_id(element)):
                continue
            target_key = (document.key, fragment)
            if target_key in self._targets:
                continue
            if anchor := self._target_anchor(element):
                self._targets[target_key] = anchor

    def _target_anchor(self, element: etree._Element) -> str | None:
        """Maps any fragment element to itself, nearest ancestor, or first descendant output target."""
        direct = self._heading_anchors.get(element) or self._note_anchors.get(element)
        if direct is not None:
            return direct
        ancestor = next(
            (parent for parent in element.iterancestors() if parent in self._heading_anchors or parent in self._note_anchors),
            None,
        )
        if ancestor is not None:
            return self._heading_anchors.get(ancestor) or self._note_anchors.get(ancestor)
        descendant = next(
            (
                child
                for child in element.iterdescendants()
                if isinstance(child.tag, str) and (child in self._heading_anchors or child in self._note_anchors)
            ),
            None,
        )
        if descendant is None:
            return None
        return self._heading_anchors.get(descendant) or self._note_anchors.get(descendant)

    def register_text_targets(
        self,
        document_key: str,
        root: etree._Element,
        sources: list[tuple[etree._Element, dict[str, object]]],
    ) -> None:
        """Bind multiple ID aliases of the actual materialized body to a block-level anchor in source order."""
        blocks_by_source = {source: block for source, block in reversed(sources)}
        for element in root.iter():
            if not isinstance(element.tag, str) or not (fragment := element_id(element)):
                continue
            block = blocks_by_source.get(element)
            if block is None:
                continue
            target_key = (document_key, fragment)
            if target_key in self._targets:
                continue
            anchor = block.get("anchor")
            if not isinstance(anchor, str):
                anchor = canonical_anchor(self._policy.anchor_prefix, document_key, f"text-{fragment}")
                block["anchor"] = anchor
            self._targets[target_key] = anchor

    def heading_anchor(self, heading: etree._Element) -> str | None:
        """Returns the specification of a registered title anchor."""
        return self._heading_anchors.get(heading)

    def heading_label(self, anchor: str) -> str | None:
        """Returns the visible title text corresponding to the canonical title anchor."""
        return self._heading_labels.get(anchor)

    def note_anchor(self, note: etree._Element) -> str | None:
        """Returns the specification anchor for a registered footnote."""
        return self._note_anchors.get(note)

    def resolve_target(self, document_key: str, fragment: str | None) -> str | None:
        """By document key with optional source fragment Returns the specification anchor without the hash mark."""
        return self._targets.get((document_key, fragment))


__all__ = [
    "AnchorTextNormalization",
    "AnchorVisibilityScope",
    "MarkupAnchorDocument",
    "MarkupAnchorPolicy",
    "MarkupAnchorRegistry",
    "canonical_anchor",
    "element_id",
    "visible_element_text",
]

# Keep the existing public type pickle path, with all old and new entries pointing to the same class.
preserve_type_module(MarkupAnchorDocument, "docvortex.analyzers.native._shared.markup.anchors")
preserve_type_module(MarkupAnchorPolicy, "docvortex.analyzers.native._shared.markup.anchors")
preserve_type_module(MarkupAnchorRegistry, "docvortex.analyzers.native._shared.markup.anchors")
