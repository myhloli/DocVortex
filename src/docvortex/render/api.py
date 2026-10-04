"""Strict MiddleJson A unified common entrance for multi-format rendering."""

from __future__ import annotations

from typing import Any, Literal, overload

from ..schema import MiddleJson

from .contracts import (
    DocxRenderOptions,
    EpubRenderOptions,
    HtmlRenderOptions,
    LatexRenderOptions,
    MarkdownRenderOptions,
    PdfRenderOptions,
    RenderFormat,
    RenderOptions,
    RenderOutput,
    StructuredContentRenderOptions,
)
from .docx import render_docx
from .epub import render_epub
from .html import render_html
from .latex import render_latex
from .markdown import render_markdown
from .pdf import render_pdf
from .structured_content import render_structured_content


@overload
def render(
    middle_json: MiddleJson,
    output_format: Literal[RenderFormat.MARKDOWN],
    *,
    options: MarkdownRenderOptions | None = None,
) -> str:
    """Declare the string return type corresponding to the Markdown target."""
    ...


@overload
def render(
    middle_json: MiddleJson,
    output_format: Literal[RenderFormat.HTML],
    *,
    options: HtmlRenderOptions | None = None,
) -> str:
    """Declare the string return type corresponding to the HTML target."""
    ...


@overload
def render(
    middle_json: MiddleJson,
    output_format: Literal[RenderFormat.LATEX],
    *,
    options: LatexRenderOptions | None = None,
) -> str:
    """Declare the string return type corresponding to the LaTeX target."""
    ...


@overload
def render(
    middle_json: MiddleJson,
    output_format: Literal[RenderFormat.DOCX],
    *,
    options: DocxRenderOptions | None = None,
) -> bytes:
    """Declare the byte return type corresponding to the DOCX target."""
    ...


@overload
def render(
    middle_json: MiddleJson,
    output_format: Literal[RenderFormat.EPUB],
    *,
    options: EpubRenderOptions | None = None,
) -> bytes:
    """Declare the byte return type corresponding to the EPUB target."""
    ...


@overload
def render(
    middle_json: MiddleJson,
    output_format: Literal[RenderFormat.PDF],
    *,
    options: PdfRenderOptions | None = None,
) -> bytes:
    """Declare the byte return type corresponding to the PDF target."""
    ...


@overload
def render(
    middle_json: MiddleJson,
    output_format: Literal[RenderFormat.STRUCTURED_CONTENT],
    *,
    options: StructuredContentRenderOptions | None = None,
) -> dict[str, Any]:
    """Declare the dictionary return type corresponding to the Structured Content target."""
    ...


def render(
    middle_json: MiddleJson,
    output_format: RenderFormat,
    *,
    options: RenderOptions | None = None,
) -> RenderOutput:
    """Render MiddleJson to native results in strict target format and corresponding options."""
    if not isinstance(middle_json, MiddleJson):
        raise TypeError("render expects a MiddleJson instance")
    if not isinstance(output_format, RenderFormat):
        raise TypeError("output_format must be a RenderFormat value")

    if output_format is RenderFormat.MARKDOWN:
        resolved_options = options if options is not None else MarkdownRenderOptions()
        if not isinstance(resolved_options, MarkdownRenderOptions):
            raise TypeError("MARKDOWN output requires MarkdownRenderOptions")
        return render_markdown(
            middle_json,
            latex_delimiters=resolved_options.latex_delimiters,
            mode=resolved_options.mode,
            asset_base_url=resolved_options.asset_base_url,
            image_renderer=resolved_options.image_renderer,
        )

    if output_format is RenderFormat.HTML:
        resolved_options = options if options is not None else HtmlRenderOptions()
        if not isinstance(resolved_options, HtmlRenderOptions):
            raise TypeError("HTML output requires HtmlRenderOptions")
        return render_html(
            middle_json,
            mode=resolved_options.mode,
            asset_base_url=resolved_options.asset_base_url,
            standalone=resolved_options.standalone,
            document_title=resolved_options.document_title,
        )

    if output_format is RenderFormat.LATEX:
        resolved_options = options if options is not None else LatexRenderOptions()
        if not isinstance(resolved_options, LatexRenderOptions):
            raise TypeError("LATEX output requires LatexRenderOptions")
        return render_latex(
            middle_json,
            asset_base_path=resolved_options.asset_base_path,
            document_title=resolved_options.document_title,
        )

    if output_format is RenderFormat.DOCX:
        resolved_options = options if options is not None else DocxRenderOptions()
        if not isinstance(resolved_options, DocxRenderOptions):
            raise TypeError("DOCX output requires DocxRenderOptions")
        return render_docx(
            middle_json,
            asset_resolver=resolved_options.asset_resolver,
        )

    if output_format is RenderFormat.EPUB:
        resolved_options = options if options is not None else EpubRenderOptions()
        if not isinstance(resolved_options, EpubRenderOptions):
            raise TypeError("EPUB output requires EpubRenderOptions")
        return render_epub(
            middle_json,
            title=resolved_options.title,
            authors=resolved_options.authors,
            language=resolved_options.language,
            identifier=resolved_options.identifier,
            modified_at=resolved_options.modified_at,
            asset_resolver=resolved_options.asset_resolver,
        )

    if output_format is RenderFormat.PDF:
        resolved_options = options if options is not None else PdfRenderOptions()
        if not isinstance(resolved_options, PdfRenderOptions):
            raise TypeError("PDF output requires PdfRenderOptions")
        return render_pdf(
            middle_json,
            asset_resolver=resolved_options.asset_resolver,
            document_title=resolved_options.document_title,
            layout=resolved_options.layout,
        )

    if output_format is RenderFormat.STRUCTURED_CONTENT:
        resolved_options = options if options is not None else StructuredContentRenderOptions()
        if not isinstance(resolved_options, StructuredContentRenderOptions):
            raise TypeError("STRUCTURED_CONTENT output requires StructuredContentRenderOptions")
        return render_structured_content(
            middle_json,
            latex_delimiters=resolved_options.latex_delimiters,
            asset_base_url=resolved_options.asset_base_url,
        )

    raise ValueError(f"Unsupported RenderFormat: {output_format}")


__all__ = ["render"]
