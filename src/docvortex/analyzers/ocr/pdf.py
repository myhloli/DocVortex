"""把独立布局和 OCR 结果转换为现有 PDF ModelJson 页面块。"""

from __future__ import annotations

from dataclasses import replace
import math
from typing import TYPE_CHECKING, Any

import numpy as np

from ...assets import encode_crop_as_jpeg_data_uri
from ...content import normalize_pdf_model_text
from ...content.spans import text_spans
from ...errors import DocumentError
from ...result import Diagnostic
from .layout import LayoutRegion
from .runtime import get_runtime

if TYPE_CHECKING:
    from ...document.pdf import PDFDocument
    from .text import TextModel

_TEXT_TYPES = {
    "abstract": "text",
    "algorithm": "algorithm",
    "aside_text": "aside_text",
    "content": "index",
    "doc_title": "doc_title",
    "figure_title": "caption",
    "footer": "footer",
    "footer_image": "footer",
    "footnote": "page_footnote",
    "header": "header",
    "header_image": "header",
    "number": "page_number",
    "paragraph_title": "paragraph_title",
    "reference": "ref_text",
    "reference_content": "ref_text",
    "text": "text",
    "vertical_text": "text",
    "vision_footnote": "footnote",
    "formula_number": "text",
}
_VISUAL_TYPES = {
    "chart": "chart",
    "display_formula": "equation",
    "image": "image",
    "seal": "image",
    "table": "table",
}


def _coverage(child: LayoutRegion, parent: LayoutRegion) -> float:
    """按子框面积计算覆盖率，用于容器认领，避免标题仅接近表格就被吸收。"""
    x0, y0, x1, y1 = child.bbox
    a0, b0, a1, b1 = parent.bbox
    intersection = max(0.0, min(x1, a1) - max(x0, a0)) * max(0.0, min(y1, b1) - max(y0, b0))
    return intersection / max((x1 - x0) * (y1 - y0), 1e-6)


def _merge_formula_number_regions(regions: list[LayoutRegion]) -> list[LayoutRegion]:
    """按保留块的相邻阅读顺序合并编号；先认领后编号，再处理尚未配对的前编号。"""
    regions = list(regions)
    pairs: dict[int, int] = {}
    numbered_formulas: set[int] = set()
    for index, region in enumerate(regions):
        if region.label == "formula_number" and index > 0 and regions[index - 1].label == "display_formula":
            pairs[index] = index - 1
            numbered_formulas.add(index - 1)
    for index, region in enumerate(regions):
        if region.label != "formula_number" or index in pairs or index + 1 >= len(regions):
            continue
        if regions[index + 1].label == "display_formula" and index + 1 not in numbered_formulas:
            pairs[index] = index + 1
            numbered_formulas.add(index + 1)
    for number_index, formula_index in pairs.items():
        formula, number = regions[formula_index], regions[number_index]
        regions[formula_index] = replace(
            formula,
            bbox=(
                min(formula.bbox[0], number.bbox[0]),
                min(formula.bbox[1], number.bbox[1]),
                max(formula.bbox[2], number.bbox[2]),
                max(formula.bbox[3], number.bbox[3]),
            ),
        )
    return [region for index, region in enumerate(regions) if index not in pairs]


def _prepare_regions(regions: list[LayoutRegion]) -> list[LayoutRegion]:
    """先过滤无效框、去重并完成容器认领，最后按阅读顺序把相邻编号纳入公式截图。"""
    regions = [
        region
        for region in regions
        if (region.label in _TEXT_TYPES or region.label in _VISUAL_TYPES)
        and len(region.bbox) == 4
        and all(math.isfinite(value) for value in region.bbox)
        and region.bbox[2] > region.bbox[0]
        and region.bbox[3] > region.bbox[1]
    ]
    claimed: set[int] = set()
    text_priority = {"doc_title": 3, "paragraph_title": 2, "text": 0, "abstract": 0}
    for index, item in enumerate(regions):
        if item.label not in _TEXT_TYPES or item.label == "reference":
            continue
        for other_index, other in enumerate(regions):
            if other_index == index or other.label not in _TEXT_TYPES or other.label == "reference":
                continue
            if _coverage(item, other) >= 0.95 and _coverage(other, item) >= 0.95:
                if (text_priority.get(other.label, 1), other.score, -other_index) > (
                    text_priority.get(item.label, 1),
                    item.score,
                    -index,
                ):
                    claimed.add(index)
                    break
    tables = [(index, item) for index, item in enumerate(regions) if item.label == "table"]
    for index, item in enumerate(regions):
        if item.label == "reference" and any(
            child.label == "reference_content" and _coverage(child, item) >= 0.8 for child in regions
        ):
            claimed.add(index)
        for owner, table in tables:
            if owner == index or _coverage(item, table) < 0.8:
                continue
            if item.label == "table" and _coverage(table, item) >= 0.8:
                # 重复表框选择更高分者；同分时保留原阅读顺序中的首项。
                if (item.score, -index) > (table.score, -owner):
                    continue
            claimed.add(index)
            break
    return _merge_formula_number_regions([region for index, region in enumerate(regions) if index not in claimed])


def _unit_bbox(bbox: tuple[float, float, float, float], size: tuple[int, int]) -> tuple[float, float, float, float]:
    """按实际渲染宽高归一化，不依赖会被长边限制改变的名义 DPI。"""
    return tuple(float(value / size[index % 2]) for index, value in enumerate(bbox))


def _analyze_page(
    rgb: np.ndarray,
    regions: list[LayoutRegion],
    text_model: TextModel,
    page_index: int,
) -> tuple[list[dict[str, Any]], list[Diagnostic]]:
    """逐块识别正文、投影表格并物化截图，所有框保持模型的阅读顺序。"""
    from ..pdf import project_table_text

    height, width = rgb.shape[:2]
    regions = _prepare_regions(regions)
    if not regions:
        regions = [LayoutRegion("text", (0.0, 0.0, float(width), float(height)), 1.0)]
    blocks, diagnostics = [], []
    for region in regions:
        x0, y0 = (max(0, math.floor(value)) for value in region.bbox[:2])
        x1, y1 = (min(limit, math.ceil(value)) for value, limit in zip(region.bbox[2:], (width, height), strict=True))
        if x1 <= x0 or y1 <= y0:
            continue
        block_type = _TEXT_TYPES.get(region.label, _VISUAL_TYPES.get(region.label))
        block: dict[str, Any] = {
            "type": block_type,
            "bbox": _unit_bbox(region.bbox, (width, height)),
            "content": "",
            "angle": 0,
        }
        if region.label in _TEXT_TYPES or region.label == "table":
            crop = rgb[y0:y1, x0:x1]
            lines = text_model.predict(crop, table=region.label == "table")
            if region.label == "table":
                block["content"] = project_table_text(
                    [[line.quad.tolist(), (line.text, line.score)] for line in lines],
                    (x1 - x0, y1 - y0),
                )
                if not block["content"]:
                    diagnostics.append(Diagnostic("ocr_table_empty", f"No table text recognized at {region.bbox}", page_index))
            else:
                block["content"] = text_spans("\n".join(line.text for line in lines))
                block["lines"] = [
                    {
                        "bbox": _unit_bbox(
                            (
                                float(line.quad[:, 0].min()) + x0,
                                float(line.quad[:, 1].min()) + y0,
                                float(line.quad[:, 0].max()) + x0,
                                float(line.quad[:, 1].max()) + y0,
                            ),
                            (width, height),
                        )
                    }
                    for line in lines
                ]
                if not lines:
                    # 页眉页脚始终保留辅助文本类型；其它非空文本区域用截图保留视觉信息。
                    is_marginal = block_type in {"header", "footer"}
                    is_blank = np.ptp(crop) == 0
                    if is_blank and not is_marginal:
                        continue
                    if not is_blank:
                        diagnostics.append(
                            Diagnostic("ocr_text_empty", f"No text recognized in {region.label} at {region.bbox}", page_index)
                        )
                    if not is_marginal:
                        block.update(type="image", content="")
                        block.pop("lines", None)
        if region.label in _VISUAL_TYPES or block["type"] == "image":
            block["image_base64"] = encode_crop_as_jpeg_data_uri(rgb, (x0, y0, x1, y1), 0)
            if not block["image_base64"]:
                raise ValueError(f"Could not encode OCR crop at {region.bbox}")
        blocks.append(block)
    return blocks, diagnostics


def analyze_pdf_ocr(
    document: PDFDocument,
    page_index_map: list[int] | None = None,
) -> tuple[list[list[dict[str, Any]]], tuple[Diagnostic, ...]]:
    """逐页分析调用方持有的 PDF，及时释放页图，诊断使用原始文档页号。"""
    layout, text_model = get_runtime()
    pages, diagnostics = [], []
    for page_index in range(document.page_count):
        source_index = page_index_map[page_index] if page_index_map else page_index
        page_image = converted = None
        try:
            page_image = document.render_page(page_index, scale=200 / 72).pil_image
            converted = page_image.convert("RGB")
            rgb = np.asarray(converted)
            regions = layout.predict(rgb)
            blocks, page_diagnostics = _analyze_page(rgb, regions, text_model, source_index)
            pages.append(blocks)
            diagnostics.extend(page_diagnostics)
        except DocumentError:
            raise
        except Exception as exc:
            raise DocumentError("ocr_inference_failed", f"OCR failed on PDF page {source_index + 1}: {exc}") from exc
        finally:
            if converted is not None:
                converted.close()
            if page_image is not None:
                page_image.close()
    normalize_pdf_model_text(pages)
    return pages, tuple(diagnostics)
