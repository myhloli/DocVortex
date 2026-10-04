"""Evidence of pages that can escape the PDFium life cycle; mutable characters materialize only at compatibility boundaries."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal

from .native_contracts import PDFLinkAnnotation, PDFPageTextGeometry, PDFPageVectorGeometry


@dataclass(frozen=True, eq=False)
class PDFPageSnapshot:
    """Holds a page of independent evidence; public character access returns a copy and does not leak mutable objects to subsequent consumers."""

    page_index: int
    page_size: tuple[float, float]
    rotation: Literal[0, 90, 180, 270]
    _geometry: PDFPageTextGeometry | None = field(repr=False, compare=False)
    _vectors: PDFPageVectorGeometry = field(repr=False, compare=False)
    _links: tuple[PDFLinkAnnotation, ...] = field(repr=False, compare=False)
    _owner: object = field(repr=False, compare=False)
    _native_text: Any = field(default=None, repr=False, compare=False)

    @property
    def text_geometry(self) -> PDFPageTextGeometry:
        """Compatible with the mutability conventions of the Python character dictionary while isolating modifications between consumers."""
        if self._native_text is not None:
            return self._native_text.materialize_geometry()
        return deepcopy(self._geometry)

    def get_lines(self, superscript_height_threshold: float = 0.7, line_distance_threshold: float = 0.1):
        """Generate base text lines directly from native snapshots, with special thresholds retaining original Python parameter semantics."""
        if (
            self._native_text is not None
            and type(superscript_height_threshold) is float
            and type(line_distance_threshold) is float
        ):
            return self._native_text.prepare_grouped_evidence(superscript_height_threshold, line_distance_threshold)[1]
        from .text import get_lines_from_chars

        return get_lines_from_chars(self.text_geometry.chars, superscript_height_threshold, line_distance_threshold)

    @property
    def vector_geometry(self) -> PDFPageVectorGeometry:
        """Return independent vector records to prevent the caller from polluting the cache after modifying nested coordinates."""
        return deepcopy(self._vectors)

    @property
    def link_annotations(self) -> tuple[PDFLinkAnnotation, ...]:
        """Return independent link evidence without visiting closed pages or documents."""
        return deepcopy(self._links)

    def validate_page(self, page: object) -> None:
        """Deny using a snapshot of another document or page for the current page."""
        document = getattr(page, "pdf_doc", None)
        if getattr(document, "_snapshot_owner", None) is not self._owner or getattr(page, "_idx", None) != self.page_index:
            raise ValueError("snapshot belongs to a different PDF page")
