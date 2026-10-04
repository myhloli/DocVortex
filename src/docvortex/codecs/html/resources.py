"""HTML wire The resource protocol that materialization depends on does not rely on a specific input parser."""

from __future__ import annotations

from typing import Protocol
from lxml import etree

from ...content.markup import MarkupContext


class WireAnchorResolver(Protocol):
    """Describes the anchor resolution capabilities provided by the precise HTML document."""

    def resolve_fragment(self, fragment: str) -> str | None:
        """Resolve source fragment as a canonical internal link."""

    def heading_anchor(self, heading: etree._Element) -> str | None:
        """Returns the canonical anchor point of the title."""

    def heading_label(self, anchor: str) -> str | None:
        """Returns the text corresponding to the title anchor."""

    def note_anchor(self, note: etree._Element) -> str | None:
        """Returns the canonical anchor point for the page footer."""


class WireResourceContext(MarkupContext, Protocol):
    """Added the ability to bind precise wire anchors to the shared markup capability."""

    def bind_anchors(self, anchors: WireAnchorResolver) -> None:
        """Binds an anchor resolver verified by the entire wire tree."""


__all__ = ["WireAnchorResolver", "WireResourceContext"]
