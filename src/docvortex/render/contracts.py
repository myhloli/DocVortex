"""MiddleJson Public types and calling options common to multiple formats renderer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re
from typing import Any, Callable, TypeAlias

from ..schema import BlockBase
from ..options import LatexDelimitersConfig


class RenderFormat(str, Enum):
    """Target formats supported by the unified rendering portal."""

    MARKDOWN = "markdown"
    HTML = "html"
    LATEX = "latex"
    DOCX = "docx"
    EPUB = "epub"
    STRUCTURED_CONTENT = "structured_content"
    PDF = "pdf"


class RenderMode(str, Enum):
    """Default merged view and full paged view shared by Markdown and HTML renderer."""

    DEFAULT = "default"
    FULL = "full"


class PdfLayout(str, Enum):
    """Automatic selection, original block layout and semantic rearrangement strategy for PDF output."""

    AUTO = "auto"
    ORIGINAL = "original"
    REFLOW = "reflow"


AssetResolver: TypeAlias = Callable[[str], bytes]
ImageRenderer: TypeAlias = Callable[[BlockBase], str]


def _validate_mode(mode: object) -> None:
    """Verify common mode parameters of Markdown and HTML renderer."""
    if not isinstance(mode, RenderMode):
        raise TypeError("mode must be a RenderMode value")


def _validate_asset_base_url(asset_base_url: object) -> None:
    """Verify the resource root address used to splice relative image paths."""
    if not isinstance(asset_base_url, str):
        raise TypeError("asset_base_url must be a string")


@dataclass(frozen=True, slots=True)
class MarkdownRenderOptions:
    """Markdown Unified entry option for renderer."""

    latex_delimiters: LatexDelimitersConfig | None = None

    mode: RenderMode = RenderMode.DEFAULT
    asset_base_url: str = ""
    image_renderer: ImageRenderer | None = None

    def __post_init__(self) -> None:
        """Reject option values that do not conform to a strict public contract at construction time."""
        _validate_mode(self.mode)
        _validate_asset_base_url(self.asset_base_url)
        if self.image_renderer is not None and not callable(self.image_renderer):
            raise TypeError("image_renderer must be callable or None")


@dataclass(frozen=True, slots=True)
class HtmlRenderOptions:
    """HTML Unified entry option for renderer."""

    mode: RenderMode = RenderMode.DEFAULT
    asset_base_url: str = ""
    standalone: bool = True
    document_title: str | None = None

    def __post_init__(self) -> None:
        """Verify HTML document shape and title options at construction time."""
        _validate_mode(self.mode)
        _validate_asset_base_url(self.asset_base_url)
        if not isinstance(self.standalone, bool):
            raise TypeError("standalone must be a bool")
        if self.document_title is not None and not isinstance(self.document_title, str):
            raise TypeError("document_title must be a string or None")


@dataclass(frozen=True, slots=True)
class LatexRenderOptions:
    """LaTeX Unified entry option for renderer."""

    asset_base_path: str = ""
    document_title: str | None = None

    def __post_init__(self) -> None:
        """Verify LaTeX material path prefix and document title during construction."""
        if not isinstance(self.asset_base_path, str):
            raise TypeError("asset_base_path must be a string")
        if self.document_title is not None and not isinstance(self.document_title, str):
            raise TypeError("document_title must be a string or None")


@dataclass(frozen=True, slots=True)
class DocxRenderOptions:
    """DOCX Unified entry option for renderer."""

    asset_resolver: AssetResolver | None = None

    def __post_init__(self) -> None:
        """Validate optional material parsers at construction time."""
        if self.asset_resolver is not None and not callable(self.asset_resolver):
            raise TypeError("asset_resolver must be callable or None")


_EPUB_LANGUAGE_RE = re.compile(r"(?:[A-Za-z]{2,8}|und)(?:-[A-Za-z0-9]{1,8})*\Z", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class EpubRenderOptions:
    """EPUB 3.3 Unified entry options for renderer."""

    title: str | None = None
    authors: tuple[str, ...] = ()
    language: str = "und"
    identifier: str | None = None
    modified_at: datetime | None = None
    asset_resolver: AssetResolver | None = None

    def __post_init__(self) -> None:
        """Verify EPUB metadata, time and material parser at construction time."""
        if self.title is not None and (not isinstance(self.title, str) or not self.title.strip()):
            raise TypeError("title must be a non-empty string or None")
        if not isinstance(self.authors, tuple) or any(
            not isinstance(author, str) or not author.strip() for author in self.authors
        ):
            raise TypeError("authors must be a tuple of non-empty strings")
        if not isinstance(self.language, str) or _EPUB_LANGUAGE_RE.fullmatch(self.language.strip()) is None:
            raise ValueError("language must be a supported BCP 47 language tag")
        if self.identifier is not None and (not isinstance(self.identifier, str) or not self.identifier.strip()):
            raise TypeError("identifier must be a non-empty string or None")
        if self.modified_at is not None:
            if not isinstance(self.modified_at, datetime):
                raise TypeError("modified_at must be a datetime or None")
            if self.modified_at.utcoffset() is None:
                raise ValueError("modified_at must be timezone-aware")
            if self.modified_at.year < 1000:
                raise ValueError("modified_at year must use four digits")
        if self.asset_resolver is not None and not callable(self.asset_resolver):
            raise TypeError("asset_resolver must be callable or None")


@dataclass(frozen=True, slots=True)
class PdfRenderOptions:
    """PDF Unified entry option for renderer."""

    asset_resolver: AssetResolver | None = None
    document_title: str | None = None
    layout: PdfLayout = PdfLayout.AUTO

    def __post_init__(self) -> None:
        """Verify asset parser and document title at construction time."""
        if not isinstance(self.layout, PdfLayout):
            raise TypeError("layout must be a PdfLayout value")
        if self.asset_resolver is not None and not callable(self.asset_resolver):
            raise TypeError("asset_resolver must be callable or None")
        if self.document_title is not None and not isinstance(self.document_title, str):
            raise TypeError("document_title must be a string or None")


@dataclass(frozen=True, slots=True)
class StructuredContentRenderOptions:
    """Tree Unified entry options for Markdown Structured Content renderer."""

    latex_delimiters: LatexDelimitersConfig | None = None

    asset_base_url: str = ""

    def __post_init__(self) -> None:
        """Verify the image resource root address during construction."""
        _validate_asset_base_url(self.asset_base_url)


RenderOptions: TypeAlias = (
    MarkdownRenderOptions
    | HtmlRenderOptions
    | LatexRenderOptions
    | DocxRenderOptions
    | EpubRenderOptions
    | PdfRenderOptions
    | StructuredContentRenderOptions
)
RenderOutput: TypeAlias = str | bytes | dict[str, Any]


__all__ = [
    "AssetResolver",
    "DocxRenderOptions",
    "EpubRenderOptions",
    "HtmlRenderOptions",
    "ImageRenderer",
    "LatexRenderOptions",
    "MarkdownRenderOptions",
    "PdfRenderOptions",
    "PdfLayout",
    "RenderFormat",
    "RenderMode",
    "RenderOptions",
    "RenderOutput",
    "StructuredContentRenderOptions",
]
