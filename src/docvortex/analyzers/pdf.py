"""面向区域分析与模型融合的 PDF 证据和表格能力。"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from ..document.pdf import PDFPage, PDFPageTextGeometry
from ..schema import BBox
from .native.pdf._script_geometry import ScriptRole, classify_char_script_roles
from .native.pdf._table_recovery.contracts import NativeTableRectangle, NativeTableRule, PDFTableRecoveryError
from .native.pdf.inline.types import (
    PDF_NATIVE_SCRIPT_MARKUP_KEY,
    PDFTextLinkLine,
    PDFTextLinkRange,
    PDFTextScriptLine,
    PDFTextScriptRange,
    PDFTextStyleLine,
    PDFTextStyleRange,
)


@dataclass(frozen=True, slots=True)
class PDFTextEvidence:
    """保存可脱离 PDF 句柄使用的页面文字证据，不暴露组行中间对象。"""

    page_size: tuple[float, float]
    geometry: PDFPageTextGeometry | None = None
    styles: tuple[PDFTextStyleLine, ...] = ()
    links: tuple[PDFTextLinkLine, ...] = ()
    scripts: tuple[PDFTextScriptLine, ...] = ()


@dataclass(frozen=True, slots=True)
class PDFTablePage:
    """一次准备并由同页多个表格区域复用的物化原语。"""

    page_size: tuple[float, float]
    geometry: PDFPageTextGeometry
    drawing_lines: tuple[NativeTableRule, ...] = ()
    rectangles: tuple[NativeTableRectangle, ...] = ()


@dataclass(frozen=True, slots=True)
class PDFTableResult:
    """返回已物化上下标的表格 HTML 及接受结果所需的诊断。"""

    html: str
    source: str
    confidence: float
    diagnostics: tuple[str, ...] = ()


def prepare_text_evidence(
    page: PDFPage,
    *,
    geometry: PDFPageTextGeometry | None = None,
    supported_angles: Sequence[float] = (0.0,),
    table_regions: Sequence[BBox] = (),
    excluded_script_regions: Sequence[BBox] = (),
) -> PDFTextEvidence:
    """读取一次字符及注释，按既定组行闭包生成页面文字证据。"""
    from ..document.pdf import get_lines_from_chars
    from .native.pdf.inline.detection import detect_pdf_text_link_lines, detect_pdf_text_style_lines
    from .native.pdf.inline.scripts import detect_pdf_text_script_lines
    from .native.pdf.line_merging import merge_text_line_clusters
    from .native.pdf.native_text import _build_native_line_items

    geometry = geometry if geometry is not None else page.get_chars_with_geometry()
    page_size = tuple(float(value) for value in page.size)
    chars = deepcopy(geometry.chars)
    lines = _build_native_line_items(
        get_lines_from_chars(chars), page_size, page_rotation=page.rotation, supported_angles=supported_angles
    )
    drawing_lines = page.get_drawing_lines()
    styles = detect_pdf_text_style_lines(lines, drawing_lines)
    links = detect_pdf_text_link_lines(lines, page.get_link_annotations())
    script_lines = merge_text_line_clusters(list(lines), page_size, list(table_regions))
    scripts = detect_pdf_text_script_lines(
        script_lines,
        page_size,
        geometry.tight_bboxes,
        geometry.origins,
        all_chars=chars,
        drawing_lines=drawing_lines,
    )
    if excluded_script_regions:
        scripts = [
            replace(
                line,
                script_ranges=tuple(
                    item
                    for item in line.script_ranges
                    if not any(
                        region[0] <= (item.bbox[0] + item.bbox[2]) / 2.0 <= region[2]
                        and region[1] <= (item.bbox[1] + item.bbox[3]) / 2.0 <= region[3]
                        for region in excluded_script_regions
                    )
                ),
            )
            for line in scripts
        ]
    return PDFTextEvidence(page_size, geometry, tuple(styles), tuple(links), tuple(scripts))


def apply_text_evidence(
    blocks: list[dict[str, Any]],
    evidence: PDFTextEvidence,
    *,
    diagnostics: list[dict[str, Any]] | None = None,
) -> None:
    """按链接、样式、上下标顺序物化目标块，保持证据和源字符不变。"""
    from .native.pdf.inline.materialize import apply_pdf_inline_evidence

    apply_pdf_inline_evidence(
        blocks,
        list(evidence.links),
        list(evidence.styles),
        list(evidence.scripts),
        evidence.page_size,
        materialized_diagnostics=diagnostics,
    )


def prepare_table_page(page: PDFPage, *, geometry: PDFPageTextGeometry | None = None) -> PDFTablePage:
    """在调用方确认存在候选表格后物化页面原语，复用已有字符几何。"""
    from .native.pdf._table_recovery.engine import coerce_native_table_rectangles, coerce_native_table_rules

    geometry = geometry if geometry is not None else page.get_chars_with_geometry()
    return PDFTablePage(
        tuple(float(value) for value in page.size),
        geometry,
        coerce_native_table_rules(page.get_drawing_lines()),
        coerce_native_table_rectangles(page.get_path_infos()),
    )


def recover_table_region(page: PDFTablePage, bbox: BBox, *, angle: int = 0) -> PDFTableResult | None:
    """恢复指定 PDF point 区域并物化上下标，不决定宿主的模型回退策略。"""
    from .native.pdf._table_recovery.contracts import NativeTableInput
    from .native.pdf.table_materialization import recover_table_result

    table_input = NativeTableInput(
        table_bbox=bbox,
        page_size=page.page_size,
        angle=angle,
        chars=tuple(page.geometry.chars),
        drawing_lines=page.drawing_lines,
        rectangles=page.rectangles,
    )
    result = recover_table_result(table_input, page.geometry.tight_bboxes, page.geometry.origins)
    if result is None:
        return None
    html, recovered = result
    return PDFTableResult(html, recovered.source, recovered.confidence, recovered.diagnostics)


def project_table_text(ocr_result: Any, table_size: tuple[int, int]) -> str:
    """按空间位置投影 OCR 表格文字，保持已有空输入和排序语义。"""
    from .native.pdf.spatial_text import project_ocr_table_text

    return project_ocr_table_text(ocr_result, table_size)


__all__ = [
    "PDFTableRecoveryError",
    "PDFTextEvidence",
    "PDFTablePage",
    "PDFTableResult",
    "prepare_text_evidence",
    "apply_text_evidence",
    "prepare_table_page",
    "recover_table_region",
    "project_table_text",
    "ScriptRole",
    "classify_char_script_roles",
    "PDF_NATIVE_SCRIPT_MARKUP_KEY",
    "PDFTextLinkLine",
    "PDFTextLinkRange",
    "PDFTextScriptLine",
    "PDFTextScriptRange",
    "PDFTextStyleLine",
    "PDFTextStyleRange",
]
