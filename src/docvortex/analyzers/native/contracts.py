"""Internal structure contract of native analysis; wire shape exposing ModelJson remains unchanged."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, BinaryIO, Protocol, TypedDict

from ...schema import BBox

if TYPE_CHECKING:
    from ...document.pdf._document import _PDFPageSnapshot


class RawBlock(TypedDict, total=False):
    """Description raw stage common fields, format-specific evidence can still be attached to the ordinary dictionary."""

    type: str
    bbox: BBox | list[float] | None
    index: int
    content: str | list[dict[str, Any]]
    angle: int
    image_base64: str
    lines: list[dict[str, list[float]]]
    _inline_math_regions: list[BBox]


class NativeBinaryAnalyzer(Protocol):
    """Limit the binary stream prediction capabilities required for API dispatch and do not create a unified inheritance framework."""

    def predict(self, file_binary: BinaryIO) -> list[list[dict[str, Any]]]:
        """Read the stream held by the caller and return to the native page without closing the stream."""


class NativePdfSource(Protocol):
    """Limit the number of pages and one-time evidence snapshots required for native PDF orchestration."""

    @property
    def page_count(self) -> int:
        """Returns the physical page number of the currently selected PDF."""

    def _extract_native_page(self, page_idx: int) -> _PDFPageSnapshot:
        """Collect native evidence during the lifetime of a single page."""


__all__ = ["RawBlock", "NativeBinaryAnalyzer", "NativePdfSource"]
