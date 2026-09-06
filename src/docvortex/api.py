"""独立文档引擎的分阶段 API 与完整转换入口。"""

from __future__ import annotations

from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
import time
from typing import TYPE_CHECKING

from .assets import AssetStore
from .document.source import HtmlSourceContext, prepare_source
from .result import AnalysisResult, Diagnostic, DocumentResult, ExportResult, RenderArtifact
from .schema import FileSuffix, MiddleJson, ModelJson, PageInfo
from .render.contracts import DocxRenderOptions, EpubRenderOptions, PdfRenderOptions, RenderFormat, RenderOptions

if TYPE_CHECKING:
    from .analyzers.native.contracts import NativeBinaryAnalyzer
    from .document.pdf.document import PDFDocument


def analyze(
    source: str | Path | bytes | PDFDocument,
    *,
    file_suffix: FileSuffix | None = None,
    page_range: str = "",
    source_context: HtmlSourceContext | None = None,
) -> AnalysisResult:
    """执行原生分析，显式调用者选择不会被隐式分类或 OCR 改写。"""
    from .analyzers.native import models

    started = time.perf_counter()
    prepared = prepare_source(source, file_suffix=file_suffix, page_range=page_range, source_context=source_context)
    try:
        if prepared.file_suffix == "pdf":
            from .document.pdf.images import load_images_from_pdf_bytes_range
            from .document.pdf.raster import estimate_page_image_bytes
            from .document.pdf.visuals import (
                _attach_prepared_visual_block_images,
                _prepare_page_visual_blocks,
                _visual_page_ranges,
            )

            assert prepared.document is not None
            pages = models.PdfModel().predict(prepared.document)
            prepared_visuals = [_prepare_page_visual_blocks(page) for page in pages]
            image_bytes = {
                index: estimate_page_image_bytes(prepared.document.page_size(index))
                for index, blocks in enumerate(prepared_visuals)
                if blocks
            }
            for start, end in _visual_page_ranges(prepared_visuals, image_bytes):
                images = load_images_from_pdf_bytes_range(
                    prepared.data, start_page_id=start, end_page_id=end, image_type="pil_img"
                )
                try:
                    _attach_prepared_visual_block_images(prepared_visuals[start : end + 1], images, page_start_index=start)
                finally:
                    for item in images:
                        if item.get("img_pil") is not None:
                            item["img_pil"].close()
        elif prepared.file_suffix == "html":
            pages = models.HtmlModel().predict(BytesIO(prepared.data), source_context=prepared.source_context)
        else:
            model_types: dict[FileSuffix, type[NativeBinaryAnalyzer]] = {
                "csv": models.CsvModel,
                "epub": models.EpubModel,
                "ofd": models.OfdModel,
                "doc": models.DocModel,
                "docx": models.DocxModel,
                "ppt": models.PptModel,
                "pptx": models.PptxModel,
                "xls": models.XlsModel,
                "xlsx": models.XlsxModel,
                "rtf": models.RtfModel,
                "odt": models.OdtModel,
                "ods": models.OdsModel,
                "odp": models.OdpModel,
            }
            pages = model_types[prepared.file_suffix]().predict(BytesIO(prepared.data))
        model = ModelJson(pages=pages, page_index_map=prepared.page_index_map or [], file_suffix=prepared.file_suffix)
        diagnostics = tuple(
            Diagnostic("broken_page", "The selected PDF page could not be loaded", index)
            for index in prepared.broken_page_indices
        )
        return AnalysisResult(model, diagnostics=diagnostics, elapsed_seconds=time.perf_counter() - started)
    finally:
        if prepared.owns_document and prepared.document is not None:
            prepared.document.close()


def postprocess(
    analysis: AnalysisResult | ModelJson, *, assets: AssetStore | None = None, keep_model_json: bool = False
) -> DocumentResult:
    """执行确定性后处理并物化结果素材，智能增强由上层显式调用。"""
    from .postprocess.document import model_json_to_middle_json
    from .export.files import materialize_middle

    if isinstance(analysis, AnalysisResult):
        model, diagnostics = analysis.model_json, analysis.diagnostics
        if assets is None:
            assets = analysis.assets
    else:
        model, diagnostics = analysis, ()
    middle = model_json_to_middle_json(model)
    if model.page_index_map:
        pages = {page.page_idx: page for page in middle.pages}
        for diagnostic in diagnostics:
            if diagnostic.code == "broken_page" and diagnostic.page_index is not None:
                pages.setdefault(diagnostic.page_index, PageInfo(page_idx=diagnostic.page_index))
        middle.pages = [pages[index] for index in sorted(pages)]
    middle, result_assets = materialize_middle(middle, assets)
    return DocumentResult(middle, result_assets, model if keep_model_json else None, diagnostics)


def parse(
    source: str | Path | bytes | PDFDocument,
    *,
    file_suffix: FileSuffix | None = None,
    page_range: str = "",
    source_context: HtmlSourceContext | None = None,
    keep_model_json: bool = False,
) -> DocumentResult:
    """完成输入、分析及后处理，返回可脱离原文件使用的结果。"""
    return postprocess(
        analyze(source, file_suffix=file_suffix, page_range=page_range, source_context=source_context),
        keep_model_json=keep_model_json,
    )


def render(
    middle_json: MiddleJson,
    output_format: RenderFormat | str,
    *,
    assets: AssetStore | None = None,
    options: RenderOptions | None = None,
) -> RenderArtifact:
    """把同一语义文档编码为目标文件，返回值不产生文件系统副作用。"""
    from .render.api import render as render_value
    from .render._internal.common.context import owned_render_document
    from .export.files import materialize_middle

    target = RenderFormat(output_format)
    middle, resolved_assets = materialize_middle(middle_json, assets)
    resolver_options = {
        RenderFormat.DOCX: DocxRenderOptions,
        RenderFormat.EPUB: EpubRenderOptions,
        RenderFormat.PDF: PdfRenderOptions,
    }
    if target in resolver_options:
        if options is None:
            options = resolver_options[target](asset_resolver=resolved_assets.__getitem__)
        elif isinstance(options, resolver_options[target]) and options.asset_resolver is None:
            options = replace(options, asset_resolver=resolved_assets.__getitem__)
    with owned_render_document(middle):
        value = render_value(middle, target, options=options)
    if isinstance(value, bytes):
        content = value
    elif isinstance(value, str):
        content = value.encode("utf-8")
    else:
        content = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    mime_types = {
        RenderFormat.MARKDOWN: "text/markdown",
        RenderFormat.HTML: "text/html",
        RenderFormat.LATEX: "application/x-tex",
        RenderFormat.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        RenderFormat.EPUB: "application/epub+zip",
        RenderFormat.PDF: "application/pdf",
    }
    packaged = target in resolver_options
    return RenderArtifact(
        content, target, mime_types.get(target, "application/json"), AssetStore() if packaged else resolved_assets
    )


def convert(
    source: str | Path | bytes | PDFDocument,
    output_path: str | Path,
    *,
    output_format: RenderFormat | str = RenderFormat.MARKDOWN,
    file_suffix: FileSuffix | None = None,
    page_range: str = "",
    source_context: HtmlSourceContext | None = None,
    options: RenderOptions | None = None,
    overwrite: bool = False,
) -> ExportResult:
    """使用完整原生流程进行一次转换，并返回实际写出的文件路径。"""
    result = parse(source, file_suffix=file_suffix, page_range=page_range, source_context=source_context)
    return result.export(output_path, output_format=output_format, options=options, overwrite=overwrite)


__all__ = ["analyze", "postprocess", "parse", "render", "convert"]
