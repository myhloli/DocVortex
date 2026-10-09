"""正文按源 PDF 原框独立绘制，标题允许借用同栏空白且不移动其他块。"""

from __future__ import annotations

from io import BytesIO
from typing import Iterable

from reportlab.platypus import Flowable, Image as ReportLabImage, Paragraph, Table

from ....schema import (
    AlgorithmBodyBlock,
    BlockBase,
    ChartBlock,
    ChartBodyBlock,
    CodeBlock,
    CodeBodyBlock,
    HyperlinkSpan,
    ImageBlock,
    ImageBodyBlock,
    ImagePayloadBlock,
    IndexBlock,
    InlineContentBlock,
    ListBlock,
    MiddleJson,
    PageAuxTextBlock,
    RefTextBlock,
    TableBlock,
    TableBodyBlock,
    TextBlock,
    TextSpan,
    TitleBlockBase,
)
from ....document.pdf.layout import LAYOUT_EXTENSION
from ...contracts import AssetResolver
from ..common.planner import PlannedBlock
from ..common.anchors import VISUAL_ANNOTATION_TYPES, visible_block_anchor
from .assets import PreparedImage
from .inline import PdfAnchorRegistry
from .font_plan import BlockFit, PreparedBlock, plan_font_sizes, record_font_plans
from .formula_layout import display_formula, place_formulas
from .title_layout import TitleContent, place_title
from .renderer import _PdfCanvas, _PdfRenderer, _flatten_non_link_spans, _has_image_payload, _plain_html_text
from .table import PdfTableError, SpatialTableOptions
from .table_layout import SpatialTableContent, has_spatial_table, place_tables


_CONTAINER_TYPES = (ImageBlock, TableBlock, ChartBlock, CodeBlock, ListBlock, IndexBlock)
_STANDARD_TABLE_BUILDER = _PdfRenderer._html_tables


class OriginalPdfRenderer(_PdfRenderer):
    """复用内容渲染器，单独负责块级布局、局部测量和页面生命周期。"""

    def __init__(
        self,
        middle_json: MiddleJson,
        *,
        asset_resolver: AssetResolver | None,
        document_title: str | None,
        page_sizes: dict[int, tuple[float, float]],
    ) -> None:
        """持有当前结果的源页尺寸，正文和素材仍由既有实现解析。"""
        super().__init__(middle_json, asset_resolver=asset_resolver, document_title=document_title)
        self.page_sizes = page_sizes
        self.image_rotations = {
            page["page_idx"]: page.get("image_rotations", {}) for page in middle_json.extensions[LAYOUT_EXTENSION]["pages"]
        }
        self.current_page_idx = 0
        self.inline_context.anchors = PdfAnchorRegistry(_original_anchors(middle_json))
        # 富标题会调整段落墨迹行高；本轮缓存只用于重排路径的普通段落。
        self.inline_context.cache_paragraphs = False
        # 固定布局的代码框不增加源文档中不存在的背景、边框和内边距。
        self.styles.code.backColor = None
        self.styles.code.borderWidth = 0
        self.styles.code.borderPadding = 0
        self.styles.spatial_table.backColor = None
        self.styles.spatial_table.borderWidth = 0
        self.styles.spatial_table.borderPadding = 0

    def render(self) -> bytes:
        """逐源页绘制所有块并保留空白页，不消费公共逻辑合并计划。"""
        output = BytesIO()
        canvas = _PdfCanvas(output, document_title=self.document_title)
        canvas.setSubject("Original block layout rendering from MiddleJson")
        blocks = []
        for page in self.middle_json.pages:
            self.current_page_idx = page.page_idx
            width, height = self.page_sizes[page.page_idx]
            for block in page.blocks:
                blocks.extend(self._prepare_block(block, page.page_idx, width, height))

        by_page = {page.page_idx: [] for page in self.middle_json.pages}
        for item in blocks:
            by_page[item.page_idx].append(item)
            if item.group is None and display_formula(item) is None and not has_spatial_table(item):
                item.fit = self._fit_block(canvas, item.flowables, item.width, item.height)
                if item.body_base_font_size is not None:
                    item.body_font_size = item.body_base_font_size * item.fit.scale

        def measure_title(item, font_size, width):
            """只测量当前标题，不修改正文的换行结果或重复物化内容。"""
            scale = font_size / item.base_font_size
            measured_width, height, measurements = self._measure(canvas, item.flowables, width / scale)
            return BlockFit(scale, measured_width * scale, height * scale, measurements, font_size < 6 - 0.001)

        def fit_small_title(item, rect):
            """空间极小时保留完整标题，使用既有低字号整体缩放兜底。"""
            return self._fit_block(
                canvas,
                item.flowables,
                rect[2] - rect[0],
                rect[3] - rect[1],
                initial_scale=min(6, item.target_font_size) / item.base_font_size,
                base_font_size=item.base_font_size,
            )

        plans = plan_font_sizes(blocks)
        by_group = {plan.group: plan for plan in plans}
        for item in blocks:
            if item.group is None:
                continue
            place_title(item, by_page[item.page_idx], self.page_sizes[item.page_idx][0], measure_title, fit_small_title)
            plan = by_group[item.group]
            final_font = item.base_font_size * item.fit.scale
            reasons = []
            if item.target_font_size > plan.target_font_size + 0.001:
                reasons.append("local_body_larger")
                plan.local_increase_count += 1
            if final_font < item.target_font_size - 0.001:
                reasons.append("insufficient_space")
            else:
                plan.fitting_count += 1
            detail = (
                f"group={item.group}, common_font_size={plan.target_font_size:.1f} pt, "
                f"body_font_size={item.reference_font_size:.4f} pt, target_font_size={item.target_font_size:.1f} pt, "
                f"final_font_size={final_font:.4f} pt, original_bbox_pt={item.original_rect}, draw_bbox_pt={item.draw_rect}"
            )
            if reasons:
                plan.exception_count += 1
                self._diagnostic(
                    "pdf_layout_font_exception", f"reason={','.join(reasons)}, {detail}", item.page_idx, item.block
                )
            if item.geometry_conflict:
                self._diagnostic("pdf_title_geometry_conflict", detail, item.page_idx, item.block)
            elif item.clearance_unavailable:
                self._diagnostic("pdf_title_clearance_unavailable", detail, item.page_idx, item.block)
            ox0, oy0, ox1, oy1 = item.original_rect
            dx0, dy0, dx1, dy1 = item.draw_rect
            expanded = dx0 < ox0 - 0.001 or dy0 < oy0 - 0.001 or dx1 > ox1 + 0.001 or dy1 > oy1 + 0.001
            if expanded:
                plan.expanded_count += 1
                self._diagnostic("pdf_title_layout_expanded", detail, item.page_idx, item.block)
            plan.titles.append(
                {
                    "page_idx": item.page_idx,
                    "block_index": item.block.index,
                    "body_font_size": item.reference_font_size,
                    "target_font_size": item.target_font_size,
                    "final_font_size": final_font,
                    "original_bbox_pt": item.original_rect,
                    "draw_bbox_pt": item.draw_rect,
                    "expanded": expanded,
                    "reasons": reasons,
                    "geometry_conflict": item.geometry_conflict,
                    "clearance_unavailable": item.clearance_unavailable,
                }
            )
        for page in self.middle_json.pages:
            place_formulas(by_page[page.page_idx], self.page_sizes[page.page_idx][0], self.styles.body.fontSize)
            place_tables(by_page[page.page_idx], self.page_sizes[page.page_idx][0], canvas)
            for prepared in self._table_contents.values():
                prepared.release_templates()
            canvas.setPageSize(self.page_sizes[page.page_idx])
            for item in by_page[page.page_idx]:
                self._draw_fitted(canvas, item)
            canvas.showPage()
            for item in by_page[page.page_idx]:
                for flow in item.flowables:
                    if isinstance(flow, SpatialTableContent):
                        flow.clear_trials()
        canvas.save()
        record_font_plans(plans)
        return output.getvalue()

    def _prepare_block(
        self,
        block: BlockBase,
        page_idx: int,
        page_width: float,
        page_height: float,
        *,
        index_entry: bool = False,
    ) -> list[PreparedBlock]:
        """物化有独立原框的叶子；缺子框的组合整体准备且不参与字号统计。"""
        if isinstance(block, _CONTAINER_TYPES):
            if all(child.bbox is not None for child in block.content):
                prepared = []
                for child in block.content:
                    prepared.extend(
                        self._prepare_block(
                            child,
                            page_idx,
                            page_width,
                            page_height,
                            index_entry=index_entry or isinstance(block, IndexBlock),
                        )
                    )
                return prepared
            self._diagnostic(
                "pdf_layout_approximate", "Missing child bbox; fitting the group inside its parent", page_idx, block
            )
        assert block.bbox is not None
        x0, y0, x1, y1 = block.bbox
        width, height = (x1 - x0) * page_width, (y1 - y0) * page_height
        self.available_width, self.available_height = width, height
        flowables = self._block_flowables(block, page_idx, index_entry=index_entry)
        item = PreparedBlock(
            page_idx, block, flowables, x0 * page_width, (1 - y1) * page_height, width, height, page_height=page_height
        )
        if isinstance(block, TitleBlockBase) and not index_entry:
            sizes = list(_paragraph_base_font_sizes(flowables))
            if sizes:
                item.base_font_size = min(sizes)
                item.group = f"{block.type}:level={block.level}"
                item.flowables = [
                    TitleContent(flowable) if isinstance(flowable, Paragraph) else flowable for flowable in flowables
                ]
        if isinstance(block, (TextBlock, RefTextBlock)) and not index_entry:
            sizes = list(_paragraph_base_font_sizes(flowables))
            if sizes:
                item.body_base_font_size = min(sizes)
        return [item]

    def _block_flowables(self, block: BlockBase, page_idx: int, *, index_entry: bool = False) -> list[Flowable]:
        """支持固定布局展开后的正文、辅助文字、视觉主体和注释叶子。"""
        if index_entry and isinstance(block, (TextBlock, TitleBlockBase)):
            spans = block.content
            if block.anchor and spans:
                spans = [HyperlinkSpan(type="hyperlink", url=f"#{block.anchor}", content=_flatten_non_link_spans(spans))]
            return [self._paragraph(spans, self.styles.body, page_idx, block)]
        if isinstance(block, TableBodyBlock):
            return [self._structured_table(block, page_idx)]
        if isinstance(block, (ImageBodyBlock, ChartBodyBlock)):
            if _has_image_payload(block):
                prepared = self._try_prepared_block_image(block, page_idx)
                if prepared is not None:
                    return [self._prepared_image_flowable(prepared, block)]
            if isinstance(block, ChartBodyBlock):
                self._diagnostic(
                    "pdf_layout_visual_fallback", "Region image unavailable; rendering structured content", page_idx, block
                )
                if "<table" in block.content.lower():
                    try:
                        return list(self._html_tables(block.content, page_idx=page_idx, block=block))
                    except PdfTableError as exc:
                        self._diagnostic("pdf_table_fallback", str(exc), page_idx, block)
                if block.content.strip():
                    return [self._preformatted(_plain_html_text(block.content), page_idx, block, self.styles.spatial_table)]
            return [self._placeholder("image unavailable", page_idx=page_idx, block=block, width=self.available_width)]
        if isinstance(block, CodeBodyBlock):
            return [self._preformatted(block.content, page_idx, block, self.styles.code)]
        if isinstance(block, AlgorithmBodyBlock):
            return [self._paragraph(block.content, self.styles.code, page_idx, block, preserve_newlines=True)]
        if isinstance(block, PageAuxTextBlock):
            return [self._paragraph(block.content, self.styles.footnote, page_idx, block)]
        if isinstance(block, InlineContentBlock) and str(block.type).endswith(("caption", "footnote")):
            # 页面脚注由原 visitor 处理，以保留其 anchor；这里只处理视觉注释。
            if str(block.type) != "page_footnote":
                return [self._render_annotation(block, page_idx)]
        if isinstance(block, (ImageBlock, TableBlock, ChartBlock, CodeBlock)):
            return [flowable for child in block.content for flowable in self._block_flowables(child, page_idx)]
        if isinstance(block, IndexBlock):
            return [
                flowable for child in block.content for flowable in self._block_flowables(child, page_idx, index_entry=True)
            ]
        return self._render_planned_block(PlannedBlock(page_idx=page_idx, block=block))

    def _structured_table(self, block: TableBodyBlock, page_idx: int) -> Flowable:
        """优先保留 HTML 网格，延迟到正文和公式定位后适配表格区域。"""
        try:
            self._table_content(block, block.content)
        except PdfTableError as exc:
            return self._table_fallback(block, page_idx, self.available_width, self.available_height, str(exc))

        def build(width: float, font_size: float) -> list[Table]:
            """用显式逻辑宽度和字号构造本次试排，不改变 renderer 的全局样式。"""
            return self._html_tables(
                block.content,
                page_idx=page_idx,
                block=block,
                available_width=width,
                spatial=SpatialTableOptions(font_size),
            )

        def fallback(width: float, height: float, reason: str) -> Flowable:
            """只在结构物化或实际绘制失败后恢复该表素材。"""
            return self._table_fallback(block, page_idx, width, height, reason)

        def validate(flowable: Flowable) -> None:
            """在独立画布预绘制，避免出错的半张表污染最终 PDF。"""
            probe = _PdfCanvas(BytesIO(), document_title=self.document_title)
            flowable.drawOn(probe, 0, 0)

        def trial_key() -> tuple | None:
            """按当前内容和样式隔离几何摘要，自定义构造回调继续执行原流程。"""
            if getattr(self._html_tables, "__func__", None) is not _STANDARD_TABLE_BUILDER:
                return None
            prepared = self._table_content(block, block.content)
            if prepared.unstyled_grids() is None:
                return None
            return (block.content, repr(vars(self.styles.table_cell)), repr(vars(self.styles.table_header)))

        def minimum_height(font_size: float) -> float | None:
            """仅标准结构表使用可证明的行高下界，自定义构造仍逐档真实测量。"""
            if getattr(self._html_tables, "__func__", None) is not _STANDARD_TABLE_BUILDER:
                return None
            return self._table_content(block, block.content).minimum_height(font_size, self.styles)

        angle = self.image_rotations.get(page_idx, {}).get(str(block.index), 0)
        return SpatialTableContent(
            build,
            fallback,
            validate,
            angle=angle,
            location=self._location(page_idx, block),
            page_idx=page_idx,
            trial_key=trial_key,
            minimum_height=minimum_height,
        )

    def _table_fallback(
        self,
        block: TableBodyBlock,
        page_idx: int,
        width: float,
        height: float,
        reason: str,
    ) -> Flowable:
        """按原图、纯文字、占位顺序兜底，图片方向只恢复一次。"""
        self._diagnostic("pdf_table_fallback", reason, page_idx, block)
        if _has_image_payload(block):
            prepared = self._try_prepared_block_image(block, page_idx)
            if prepared is not None:
                angle = self.image_rotations.get(page_idx, {}).get(str(block.index), 0)
                ratio = prepared.width_px / prepared.height_px if angle in (90, 270) else prepared.height_px / prepared.width_px
                image_height = width * ratio
                scale = min(1.0, height / max(image_height, 0.001))
                self._diagnostic("pdf_table_source", f"source=image, angle={angle}", page_idx, block)
                return _RegionImage(prepared.data, width * scale, image_height * scale, angle)
        if block.content.strip():
            self._diagnostic("pdf_table_source", "source=text", page_idx, block)
            return self._paragraph(
                [TextSpan(type="text", content=_plain_html_text(block.content))],
                self.styles.spatial_table,
                page_idx,
                block,
                preserve_newlines=True,
                max_width=width,
            )
        self._diagnostic("pdf_table_source", "source=placeholder", page_idx, block)
        return self._placeholder("table unavailable", block=block, page_idx=page_idx, width=width)

    def _prepared_image_flowable(
        self,
        prepared: PreparedImage,
        block: ImagePayloadBlock | None,
        *,
        max_width: float | None = None,
    ) -> Flowable:
        """将区域素材直接映射到块宽度，避免再次乘归一化 bbox 宽度。"""
        width = self.available_width if max_width is None else max_width
        angle = self.image_rotations.get(self.current_page_idx, {}).get(str(block.index), 0) if block is not None else 0
        height = width * (
            prepared.width_px / prepared.height_px if angle in (90, 270) else prepared.height_px / prepared.width_px
        )
        scale = min(1.0, self.available_height / height)
        return _RegionImage(prepared.data, width * scale, height * scale, angle)

    def _placeholder(
        self,
        label: str,
        *,
        block: BlockBase,
        page_idx: int,
        width: float | None = None,
        url: str | None = None,
    ) -> Paragraph:
        """用可测量段落展示缺失内容，避免固定高度的占位表格产生文字外溢。"""
        self._diagnostic("pdf_content_placeholder", label, page_idx, block)
        spans = [TextSpan(type="text", content=label[:320] or "content unavailable")]
        if url:
            spans = [HyperlinkSpan(type="hyperlink", url=url, content=spans)]
        paragraph = self._paragraph(spans, self.styles.placeholder, page_idx, block, max_width=width)
        paragraph._pdf_layout_placeholder = True
        return paragraph

    def _fit_block(
        self,
        canvas: _PdfCanvas,
        flowables: list[Flowable],
        width: float,
        height: float,
        *,
        initial_scale: float = 1.0,
        base_font_size: float | None = None,
    ) -> BlockFit:
        """沿用既有逐块适配算法，只返回测量结果，供正文冻结和标题低字号兜底。"""
        if not flowables:
            return BlockFit(initial_scale, 0, 0, [])
        font_sizes = list(_paragraph_font_sizes(flowables))
        minimum_size = base_font_size or (min(font_sizes) if font_sizes else 6.0)
        minimum_scale = min(1.0, 6.0 / minimum_size)
        scale = initial_scale
        small_text = False
        while True:
            measured_width, measured_height, measurements = self._measure(canvas, flowables, width / scale)
            if measured_width * scale <= width + 0.001 and measured_height * scale <= height + 0.001:
                break
            if scale <= minimum_scale:
                scale = min(scale, width / max(measured_width, 0.001), height / max(measured_height, 0.001))
                small_text = bool(font_sizes and min(font_sizes) * scale < 6.0)
                break
            scale = max(minimum_scale, scale * 0.9)
        return BlockFit(scale, measured_width * scale, measured_height * scale, measurements, small_text)

    def _draw_fitted(self, canvas: _PdfCanvas, item: PreparedBlock) -> None:
        """只绘制已经确认的测量结果，正文不重新排版，标题使用独立的安全绘制框。"""
        fit = item.fit
        if fit.small_text:
            self._diagnostic(
                "pdf_layout_small_text", f"Block fitted below 6 pt (scale={fit.scale:.4f})", item.page_idx, item.block
            )
        if fit.scale < 0.999:
            self._diagnostic("pdf_layout_scaled", f"Block fitted at scale={fit.scale:.4f}", item.page_idx, item.block)
        rect = item.draw_rect or item.original_rect
        canvas.saveState()
        try:
            canvas.translate(rect[0], item.page_height - rect[1])
            canvas.scale(fit.scale, fit.scale)
            cursor = 0.0
            for flowable, _width, item_height, gap in fit.measurements:
                cursor -= gap + item_height
                flowable.drawOn(canvas, 0, cursor)
        finally:
            canvas.restoreState()

    def _measure(
        self,
        canvas: _PdfCanvas,
        flowables: list[Flowable],
        width: float,
    ) -> tuple[float, float, list[tuple[Flowable, float, float, float]]]:
        """测量同一组内容的换行结果，忽略块外边距并保留组内间距。"""
        measurements: list[tuple[Flowable, float, float, float]] = []
        height = max_width = previous_after = 0.0
        for index, flowable in enumerate(flowables):
            item_width, item_height = flowable.wrapOn(canvas, width, 1e9)
            gap = max(previous_after, flowable.getSpaceBefore()) if index else 0.0
            height += gap + item_height
            max_width = max(max_width, item_width)
            previous_after = flowable.getSpaceAfter()
            measurements.append((flowable, item_width, item_height, gap))
        return max_width, height, measurements


def _paragraph_base_font_sizes(flowables: Iterable[Flowable]) -> Iterable[float]:
    """只统计真实文字的基础样式字号，排除空段、占位符和上下标相对字号。"""
    for flowable in _paragraphs(flowables):
        has_inline_visual = any(getattr(getattr(fragment, "cbDefn", None), "width", 0) for fragment in flowable.frags)
        if not getattr(flowable, "_pdf_layout_placeholder", False) and (
            flowable.getPlainText().strip(" \n\t\r\u200b") or has_inline_visual
        ):
            yield flowable.style.fontSize


def _paragraphs(flowables: Iterable[Flowable]) -> Iterable[Paragraph]:
    """遍历段落及嵌套表格单元格，供基础字号和低可读性检查共同使用。"""
    for flowable in flowables:
        if isinstance(flowable, Paragraph):
            yield flowable
        elif isinstance(flowable, TitleContent):
            yield flowable.paragraph
        for row in getattr(flowable, "_cellvalues", ()):
            for cell in row:
                if isinstance(cell, (list, tuple)):
                    yield from _paragraphs(cell)
                elif isinstance(cell, Flowable):
                    yield from _paragraphs([cell])


def _paragraph_font_sizes(flowables: Iterable[Flowable]) -> Iterable[float]:
    """读取段落及表格内的实际文字字号，用于判断局部缩放后的可读性。"""
    for flowable in _paragraphs(flowables):
        yield flowable.style.fontSize
        for fragment in flowable.frags:
            size = getattr(fragment, "fontSize", None)
            if isinstance(size, (int, float)) and size > 0:
                yield size


def _original_anchors(middle: MiddleJson) -> Iterable[str]:
    """收集实际正文目标而不把目录中的引用误注册为目标。"""
    pending = [block for page in middle.pages for block in page.blocks][::-1]
    while pending:
        block = pending.pop()
        if isinstance(block, IndexBlock):
            continue
        anchor = visible_block_anchor(block) if isinstance(block, VISUAL_ANNOTATION_TYPES) else getattr(block, "anchor", None)
        if anchor:
            yield anchor
        if isinstance(block, _CONTAINER_TYPES):
            pending.extend(reversed(block.content))


class _RegionImage(Flowable):
    """保留既有转正素材，通过 Canvas 变换恢复源页方向，避免再次编码图片。"""

    def __init__(self, data: bytes, width: float, height: float, angle: int) -> None:
        """保存最终显示尺寸，内部图片尺寸随四分之一转向交换。"""
        super().__init__()
        self.width, self.height, self.angle = width, height, angle
        image_width, image_height = (height, width) if angle in (90, 270) else (width, height)
        self.image = ReportLabImage(BytesIO(data), width=image_width, height=image_height)

    def draw(self) -> None:
        """撤销裁图转正的角度，显示矩形仍严格位于当前块框内。"""
        if self.angle == 90:
            self.canv.translate(0, self.height)
            self.canv.rotate(-90)
        elif self.angle == 270:
            self.canv.translate(self.width, 0)
            self.canv.rotate(90)
        elif self.angle == 180:
            self.canv.translate(self.width, self.height)
            self.canv.rotate(180)
        self.image.drawOn(self.canv, 0, 0)
