"""Strictly public rendering implementation of MiddleJson to ReportLab PDF bytes."""

from __future__ import annotations

from functools import partial
import html
from io import BytesIO
import re
from typing import Any, Iterable

from bs4 import BeautifulSoup
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    Image as ReportLabImage,
    Paragraph,
    PageTemplate,
    Spacer,
    Table,
    TableStyle,
)

from ....content.inline import inline_plain_text, join_inline_spans
from ..common.index import strip_index_page_tail
from ..common.list_items import parse_list_item_marker, reference_list_needs_bullets
from ..common.planner import PlannedBlock, build_render_plan
from ...contracts import AssetResolver, PdfLayout
from ....schema import (
    PAGE_AUXILIARY_BLOCK_TYPES,
    RAW_ALGORITHM,
    AlgorithmBodyBlock,
    BlockBase,
    BlockType,
    ChartAnnotationBlock,
    ChartBlock,
    ChartBodyBlock,
    CodeAnnotationBlock,
    CodeBlock,
    CodeBodyBlock,
    DocTitleBlock,
    EquationBlock,
    HyperlinkSpan,
    ImageAnnotationBlock,
    ImageBlock,
    ImageBodyBlock,
    ImagePayloadBlock,
    IndexBlock,
    InlineSpan,
    ListBlock,
    MiddleJson,
    NonLinkInlineSpan,
    PageFootnoteBlock,
    ParagraphTitleBlock,
    RefTextBlock,
    TableAnnotationBlock,
    TableBlock,
    TableBodyBlock,
    TextBlock,
    TextSpan,
    TitleBlockBase,
)
from .assets import PdfAssetError, PreparedImage, prepare_block_image, prepare_html_image
from .diagnostics import report_pdf_diagnostic
from .formula import (
    DisplayFormulaFlowable,
    FormulaRenderer,
    InlineFormulaImage,
    PdfFormulaError,
    draw_inline_formula,
    split_formula_tag,
)
from .inline import PdfAnchorRegistry, PdfInlineContext, build_pdf_paragraph, render_plain_text_markup
from .table_content import PdfTableContent
from .pagination import protect_images, protect_paragraphs, protect_tables
from .styles import BORDER_COLOR, FRAME_PADDING, PAGE_MARGIN, SURFACE_COLOR, build_pdf_styles
from .table import PdfTableError, SpatialTableOptions, build_pdf_tables

_HTML_TABLE_RE = re.compile(r"<table\b", re.IGNORECASE)
_INVALID_METADATA_TEXT_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff\ufffe\uffff]")
_VISIBLE_HTML_TAGS = (
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "div",
    "em",
    "eq",
    "i",
    "li",
    "ol",
    "p",
    "pre",
    "span",
    "strong",
    "sub",
    "sup",
    "table",
    "u",
    "ul",
)
_MAX_PLACEHOLDER_TEXT = 320
_MIN_IMAGE_WIDTH = 5 * mm
_PLACEHOLDER_HEIGHT = 18 * mm


class _PdfCanvas(Canvas):
    """Provides deterministic metadata with inline ZiaMath vector drawing of ReportLab and Canvas."""

    def __init__(self, filename: Any, *args: Any, document_title: str, **kwargs: Any) -> None:
        """Create PDF with compression enabled and invariant canvas, and write stable metadata."""
        kwargs.setdefault("pageCompression", 1)
        kwargs["invariant"] = 1
        super().__init__(filename, *args, **kwargs)
        self.setTitle(document_title)
        self.setAuthor("DocVortex")
        self.setCreator("DocVortex PDF Renderer")
        self.setSubject("Semantic document rendering from MiddleJson")
        self.setKeywords("DocVortex, MiddleJson, PDF")

    def drawImage(
        self,
        image: Any,
        x: float,
        y: float,
        width: float | None = None,
        height: float | None = None,
        mask: Any = None,
        preserveAspectRatio: bool = False,
        anchor: str = "c",
        anchorAtXY: bool = False,
        showBoundary: bool = False,
    ) -> Any:
        """Recognizes Paragraph incoming formula proxies, otherwise follows standard raster picture behavior."""
        if isinstance(image, InlineFormulaImage):
            resolved_width = image.vector.width if width is None else float(width)
            resolved_height = image.vector.height if height is None else float(height)
            return draw_inline_formula(self, image, x, y, resolved_width, resolved_height)
        return super().drawImage(
            image,
            x,
            y,
            width,
            height,
            mask=mask,
            preserveAspectRatio=preserveAspectRatio,
            anchor=anchor,
            anchorAtXY=anchorAtXY,
            showBoundary=showBoundary,
        )


class _PdfRenderer:
    """Maintain the styles, formulas and material status required for rendering MiddleJson to PDF once."""

    def __init__(
        self,
        middle_json: MiddleJson,
        *,
        asset_resolver: AssetResolver | None,
        document_title: str | None,
    ) -> None:
        """Save strict typing and pre-register title and page footer anchor."""
        self.middle_json = middle_json
        self.asset_resolver = asset_resolver
        self.document_title = _resolve_document_title(middle_json, document_title)
        self.styles = build_pdf_styles()
        self._table_contents: dict[int, PdfTableContent] = {}
        self.available_width = A4[0] - 2 * (PAGE_MARGIN + FRAME_PADDING)
        self.available_height = A4[1] - 2 * (PAGE_MARGIN + FRAME_PADDING)
        self.inline_context = PdfInlineContext(
            formulas=FormulaRenderer(),
            anchors=PdfAnchorRegistry(_iter_document_anchors(middle_json)),
            cache_paragraphs=True,
        )

    def render(self) -> bytes:
        """Constructs a page-by-page story and serializes the deterministic ReportLab document into a bytes."""
        story: list[Flowable] = []
        measure_canvas = Canvas(BytesIO())
        pagination_options = {
            "width": self.available_width,
            "height": self.available_height,
            "canvas": measure_canvas,
        }
        planned_pages = build_render_plan(self.middle_json)
        for planned_blocks in planned_pages:
            for planned in planned_blocks:
                if planned.removed:
                    continue
                if planned.block.type in PAGE_AUXILIARY_BLOCK_TYPES:
                    continue
                rendered = self._render_planned_block(planned)
                if isinstance(planned.block, (ImageBlock, TableBlock, ChartBlock)):
                    # The title is included in the budget of the first paragraph of the chart. Avoid leaving the title alone on the front page with a full page of pictures or a long table.
                    while story and isinstance(story[-1], Paragraph) and story[-1].getKeepWithNext():
                        rendered.insert(0, story.pop())
                if isinstance(planned.block, (TextBlock, RefTextBlock, ListBlock)):
                    rendered = protect_paragraphs(rendered, **pagination_options)
                elif isinstance(planned.block, ImageBlock):
                    rendered = protect_images(rendered, **pagination_options)
                elif isinstance(planned.block, (TableBlock, ChartBlock)):
                    if any(isinstance(item, ReportLabImage) for item in rendered):
                        rendered = protect_images(rendered, **pagination_options)
                    else:
                        rendered = protect_tables(rendered, **pagination_options)
                story.extend(rendered)
        if not story:
            story.append(Spacer(1, 1))
        # Rearrangement story has been completely materialized, and paging only requires the actual Flowable, and there is no need to continue to retain the HTML parsing cache.
        self._table_contents.clear()

        output = BytesIO()
        document = BaseDocTemplate(
            output,
            pagesize=A4,
            leftMargin=PAGE_MARGIN,
            rightMargin=PAGE_MARGIN,
            topMargin=PAGE_MARGIN,
            bottomMargin=PAGE_MARGIN,
            title=self.document_title,
            author="DocVortex",
            creator="DocVortex PDF Renderer",
            subject="Semantic document rendering from MiddleJson",
            keywords="DocVortex, MiddleJson, PDF",
            invariant=1,
            pageCompression=1,
        )
        document.addPageTemplates(
            PageTemplate(
                id="normal",
                frames=[
                    Frame(
                        PAGE_MARGIN,
                        PAGE_MARGIN,
                        A4[0] - 2 * PAGE_MARGIN,
                        A4[1] - 2 * PAGE_MARGIN,
                        leftPadding=FRAME_PADDING,
                        rightPadding=FRAME_PADDING,
                        topPadding=FRAME_PADDING,
                        bottomPadding=FRAME_PADDING,
                        id="normal",
                    )
                ],
            )
        )
        document.build(
            story,
            canvasmaker=partial(_PdfCanvas, document_title=self.document_title),
        )
        return output.getvalue()

    def _render_planned_block(self, planned: PlannedBlock) -> list[Flowable]:
        """Dispatched by the exact PageBlock specific type PDF Flowable visitor."""
        block = planned.block
        if isinstance(block, (TextBlock, RefTextBlock)):
            spans = join_inline_spans(planned.text_contents or [block.content])
            anchor = block.anchor if isinstance(block, TextBlock) else None
            return [self._paragraph(spans, self.styles.body, planned.page_idx, block, anchor=anchor)]
        if isinstance(block, (DocTitleBlock, ParagraphTitleBlock)):
            return [
                self._paragraph(
                    block.content,
                    self.styles.heading(block.level),
                    planned.page_idx,
                    block,
                    anchor=block.anchor,
                )
            ]
        if isinstance(block, PageFootnoteBlock):
            return [
                self._paragraph(
                    block.content,
                    self.styles.footnote,
                    planned.page_idx,
                    block,
                    anchor=block.anchor,
                )
            ]
        if isinstance(block, EquationBlock):
            return self._render_equation(block, planned.page_idx)
        if isinstance(block, ListBlock):
            return self._render_list(block, planned.page_idx, depth=0)
        if isinstance(block, IndexBlock):
            return self._render_index(block, planned.page_idx, depth=0)
        if isinstance(block, ImageBlock):
            return self._render_image_block(block, planned.page_idx)
        if isinstance(block, TableBlock):
            return self._render_table_block(block, planned.page_idx)
        if isinstance(block, ChartBlock):
            return self._render_chart_block(block, planned.page_idx)
        if isinstance(block, CodeBlock):
            return self._render_code_block(block, planned.page_idx)
        raise TypeError(f"Unsupported PageBlock type: {type(block).__name__}")

    def _paragraph(
        self,
        spans: list[InlineSpan],
        style: ParagraphStyle,
        page_idx: int,
        block: BlockBase,
        *,
        anchor: str | None = None,
        preserve_newlines: bool = False,
        max_width: float | None = None,
    ) -> Paragraph:
        """Constructs a Paragraph with positioning, anchor and vector formulas for the current block."""
        return build_pdf_paragraph(
            spans,
            style,
            context=self.inline_context,
            page_idx=page_idx,
            block_index=block.index,
            block_type=str(block.type),
            max_width=self.available_width if max_width is None else max_width,
            anchor=anchor,
            preserve_newlines=preserve_newlines,
        )

    def _render_equation(self, block: EquationBlock, page_idx: int) -> list[Flowable]:
        """Priority is given to outputting the ZiaMath display vector. After failure, use pictures, LaTeX or placeholders."""
        content = block.content.strip()
        if content:
            formula, tag = split_formula_tag(content)
            try:
                font_size = self.styles.body.fontSize
                vector = self.inline_context.formulas.render(formula or content, inline=False, font_size=font_size)
                tag_vector = None
                if tag:
                    # Ordinary numbering uses traditional Chinese font; explicit mathematical commands in numbering are still processed by the original LaTeX.
                    tag_latex = rf"\mathrm{{({tag})}}" if re.fullmatch(r"[\w .,:+\-/]+", tag) else f"({tag})"
                    tag_vector = self.inline_context.formulas.render(tag_latex, inline=True, font_size=font_size)
                flowable = DisplayFormulaFlowable(
                    vector, tag_vector, font_size=font_size, location=self._location(page_idx, block), page_index=page_idx
                )
                flowable.spaceBefore = 5
                flowable.spaceAfter = 7
                return [flowable]
            except PdfFormulaError as exc:
                self._diagnostic("pdf_formula_fallback", str(exc), page_idx, block)
        if _has_image_payload(block):
            image = self._try_prepared_block_image(block, page_idx)
            if image is not None:
                return [self._prepared_image_flowable(image, block)]
        if content:
            return [
                self._paragraph(
                    [TextSpan(type="text", content=content)],
                    self.styles.formula_fallback,
                    page_idx,
                    block,
                    preserve_newlines=True,
                )
            ]
        return [self._placeholder("formula unavailable", block=block, page_idx=page_idx)]

    def _render_list(self, block: ListBlock, page_idx: int, *, depth: int) -> list[Flowable]:
        """Reserved producer marker expresses recursive lists with indentation and hanging indentation."""
        rendered: list[Flowable] = []
        add_reference_bullets = reference_list_needs_bullets(block)
        for child in block.content:
            if isinstance(child, ListBlock):
                rendered.extend(self._render_list(child, page_idx, depth=depth + 1))
                continue
            spans = list(child.content)
            parsed = parse_list_item_marker(spans)
            if add_reference_bullets and parsed.marker is None and inline_plain_text(spans).strip():
                spans = [TextSpan(type="text", content="- "), *spans]
                parsed = parse_list_item_marker(spans)
            style = ParagraphStyle(
                f"DocVortex PDF List {depth} {len(rendered)}",
                parent=self.styles.body,
                leftIndent=(depth + (1 if parsed.marker else 0)) * 14,
                firstLineIndent=-14 if parsed.marker else 0,
                spaceAfter=3,
            )
            rendered.append(self._paragraph(spans, style, page_idx, child))
        return rendered

    def _render_index(self, block: IndexBlock, page_idx: int, *, depth: int) -> list[Flowable]:
        """Recursively output directory leaves and write the registered title anchor as an internal link."""
        rendered: list[Flowable] = []
        for child in block.content:
            if isinstance(child, IndexBlock):
                rendered.extend(self._render_index(child, page_idx, depth=depth + 1))
                continue
            content = strip_index_page_tail(child.content)
            if not content:
                continue
            spans: list[InlineSpan] = [TextSpan(type="text", content="• ")]
            if child.anchor:
                link_content = _flatten_non_link_spans(content)
                if link_content:
                    spans.append(HyperlinkSpan(type="hyperlink", url=f"#{child.anchor}", content=link_content))
            else:
                spans.extend(content)
            style = ParagraphStyle(
                f"DocVortex PDF Index {depth} {len(rendered)}",
                parent=self.styles.body,
                leftIndent=(depth + 1) * 14,
                firstLineIndent=-10,
                spaceAfter=3,
            )
            rendered.append(self._paragraph(spans, style, page_idx, child))
        return rendered

    def _render_image_block(self, block: ImageBlock, page_idx: int) -> list[Flowable]:
        """Output the main body of the image, loose placeholder, and description in original sub-block order."""
        rendered: list[Flowable] = []
        for child in block.content:
            if isinstance(child, ImageBodyBlock):
                alt_text = _plain_html_text(child.content) or block.sub_type or "image"
                flowable, succeeded = self._image_or_placeholder(child, page_idx=page_idx, alt_text=alt_text)
                rendered.append(flowable)
                if not succeeded and child.content.strip():
                    rendered.append(
                        self._paragraph(
                            [TextSpan(type="text", content=_plain_html_text(child.content))],
                            self.styles.footnote,
                            page_idx,
                            child,
                        )
                    )
            elif isinstance(child, ImageAnnotationBlock):
                rendered.append(self._render_annotation(child, page_idx))
            else:
                raise TypeError(f"Unsupported image child: {type(child).__name__}")
        return rendered

    def _render_table_block(self, block: TableBlock, page_idx: int) -> list[Flowable]:
        """Prioritize the output of native HTML table, and then fall back to space text, pictures or placeholders."""
        rendered: list[Flowable] = []
        for child in block.content:
            if isinstance(child, TableBodyBlock):
                content = child.content.strip()
                if content and _HTML_TABLE_RE.search(content):
                    try:
                        rendered.extend(self._html_tables(content, page_idx=page_idx, block=child))
                        continue
                    except PdfTableError as exc:
                        self._diagnostic("pdf_table_fallback", str(exc), page_idx, child)
                    if _has_image_payload(child):
                        image = self._try_prepared_block_image(child, page_idx)
                        if image is not None:
                            rendered.append(self._prepared_image_flowable(image, child))
                            continue
                    plain = _plain_html_text(content)
                    if plain:
                        rendered.append(self._preformatted(plain, page_idx, child, self.styles.spatial_table))
                    rendered.append(self._placeholder("table unavailable", block=child, page_idx=page_idx))
                elif content:
                    rendered.append(self._preformatted(child.content, page_idx, child, self.styles.spatial_table))
                else:
                    flowable, _succeeded = self._image_or_placeholder(child, page_idx=page_idx, alt_text="table")
                    rendered.append(flowable)
            elif isinstance(child, TableAnnotationBlock):
                rendered.append(self._render_annotation(child, page_idx))
            else:
                raise TypeError(f"Unsupported table child: {type(child).__name__}")
        return rendered

    def _render_chart_block(self, block: ChartBlock, page_idx: int) -> list[Flowable]:
        """Outputs a chart image or placeholder and continues to retain materializable structured content."""
        rendered: list[Flowable] = []
        for child in block.content:
            if isinstance(child, ChartBodyBlock):
                has_image = _has_image_payload(child)
                image_succeeded = False
                if has_image:
                    flowable, image_succeeded = self._image_or_placeholder(
                        child,
                        page_idx=page_idx,
                        alt_text=block.sub_type or "chart",
                    )
                    rendered.append(flowable)
                content = child.content.strip()
                if content and _HTML_TABLE_RE.search(content):
                    try:
                        rendered.extend(self._html_tables(content, page_idx=page_idx, block=child))
                    except PdfTableError as exc:
                        self._diagnostic("pdf_table_fallback", str(exc), page_idx, child)
                        if not image_succeeded:
                            rendered.append(self._preformatted(_plain_html_text(content), page_idx, child, self.styles.body))
                elif content and not image_succeeded:
                    rendered.append(
                        self._paragraph(
                            [TextSpan(type="text", content=_plain_html_text(content))],
                            self.styles.body,
                            page_idx,
                            child,
                        )
                    )
                elif not has_image and not content:
                    rendered.append(self._placeholder("chart unavailable", block=child, page_idx=page_idx))
            elif isinstance(child, ChartAnnotationBlock):
                rendered.append(self._render_annotation(child, page_idx))
            else:
                raise TypeError(f"Unsupported chart child: {type(child).__name__}")
        return rendered

    def _render_code_block(self, block: CodeBlock, page_idx: int) -> list[Flowable]:
        """Use light blocks of equal width to output code or algorithm text with vector formulas."""
        rendered: list[Flowable] = []
        for child in block.content:
            if isinstance(child, CodeBodyBlock):
                if block.sub_type != BlockType.CODE:
                    raise TypeError("code_body requires code subtype")
                rendered.append(
                    self._paragraph(
                        [TextSpan(type="text", content=child.content or " ")],
                        self.styles.code,
                        page_idx,
                        child,
                        preserve_newlines=True,
                    )
                )
            elif isinstance(child, AlgorithmBodyBlock):
                if block.sub_type != RAW_ALGORITHM:
                    raise TypeError("algorithm_body requires algorithm subtype")
                rendered.append(
                    self._paragraph(
                        child.content,
                        self.styles.code,
                        page_idx,
                        child,
                        preserve_newlines=True,
                    )
                )
            elif isinstance(child, CodeAnnotationBlock):
                rendered.append(self._render_annotation(child, page_idx))
            else:
                raise TypeError(f"Unsupported code child: {type(child).__name__}")
        return rendered

    def _render_annotation(
        self,
        block: ImageAnnotationBlock | TableAnnotationBlock | ChartAnnotationBlock | CodeAnnotationBlock,
        page_idx: int,
    ) -> Paragraph:
        """Select the weakening description style according to caption/footnote discriminator."""
        style = self.styles.caption if str(block.type).endswith("caption") else self.styles.footnote
        return self._paragraph(block.content, style, page_idx, block)

    def _preformatted(
        self,
        content: str,
        page_idx: int,
        block: BlockBase,
        style: ParagraphStyle,
    ) -> Paragraph:
        """Write an ordinary string that needs to preserve newlines and whitespace as Paragraph."""
        return self._paragraph(
            [TextSpan(type="text", content=content or " ")],
            style,
            page_idx,
            block,
            preserve_newlines=True,
        )

    def _table_content(self, block: BlockBase, content: str) -> PdfTableContent:
        """Prepare the HTML content of each block once to avoid reusing positioning context across blocks."""
        prepared = self._table_contents.get(id(block))
        if prepared is None or prepared.source != content:
            prepared = self._table_contents[id(block)] = PdfTableContent(content)
        return prepared

    def _html_tables(
        self,
        content: str,
        *,
        page_idx: int,
        block: BlockBase,
        available_width: float | None = None,
        spatial: SpatialTableOptions | None = None,
    ) -> list[Table]:
        """Materializes HTML table into a ReportLab table using the current block context."""

        def build_paragraph(spans: list[InlineSpan], style: object, max_width: float) -> Paragraph:
            """Constructs a Paragraph for table cells that supports formulas and links."""
            if not isinstance(style, ParagraphStyle):
                raise TypeError("PDF table paragraph style must be a ParagraphStyle")
            return self._paragraph(
                spans,
                style,
                page_idx,
                block,
                preserve_newlines=True,
                # The original format table uses formula natural width to participate in column width constraints, and finally unifies the zoom area to avoid scaling twice in advance.
                max_width=max_width if spatial is None else float("inf"),
            )

        def build_image(source: str, max_width: float, alt_text: str) -> Flowable:
            """Construct offline pictures or loose placeholders for table cells."""
            try:
                prepared = prepare_html_image(source, self.asset_resolver)
            except PdfAssetError as exc:
                self._diagnostic("pdf_image_unavailable", str(exc), page_idx, block)
                return self._placeholder(
                    f"image unavailable: {alt_text}",
                    block=block,
                    page_idx=page_idx,
                    width=max_width,
                    url=source if _is_remote_url(source) else None,
                )
            if spatial is not None:
                # Cell pictures use logical coordinates independently and do not inherit the rotation or outer height limit of the entire area map.
                width = min(max_width, prepared.width_px * 0.75)
                return ReportLabImage(
                    BytesIO(prepared.data), width=width, height=width * prepared.height_px / prepared.width_px
                )
            return self._prepared_image_flowable(prepared, None, max_width=max_width)

        return list(
            build_pdf_tables(
                content,
                available_width=self.available_width if available_width is None else available_width,
                styles=self.styles,
                build_paragraph=build_paragraph,
                build_image=build_image,
                spatial=spatial,
                prepared=self._table_content(block, content),
            )
        )

    def _image_or_placeholder(
        self,
        block: ImagePayloadBlock,
        *,
        page_idx: int,
        alt_text: str,
    ) -> tuple[Flowable, bool]:
        """Load the block image; any offline failures are converted to visible placeholders without throwing."""
        prepared = self._try_prepared_block_image(block, page_idx)
        if prepared is not None:
            return self._prepared_image_flowable(prepared, block), True
        remote_url = block.image_url if block.image_path is None and block.image_base64 is None else None
        return (
            self._placeholder(
                f"image unavailable: {alt_text}",
                block=block,
                page_idx=page_idx,
                url=remote_url,
            ),
            False,
        )

    def _try_prepared_block_image(self, block: ImagePayloadBlock, page_idx: int) -> PreparedImage | None:
        """Try preparing images offline and record material errors as DEBUG diagnostics."""
        try:
            return prepare_block_image(block, self.asset_resolver)
        except PdfAssetError as exc:
            self._diagnostic("pdf_image_unavailable", str(exc), page_idx, block)
            return None

    def _prepared_image_flowable(
        self,
        prepared: PreparedImage,
        block: ImagePayloadBlock | None,
        *,
        max_width: float | None = None,
    ) -> ReportLabImage:
        """Constrains the image to its natural size or bbox width, maintaining aspect ratio and left alignment."""
        available_width = self.available_width if max_width is None else max(1.0, max_width)
        natural_width = prepared.width_px / 96 * 72
        desired_width = natural_width
        if block is not None and block.bbox is not None:
            desired_width = available_width * (block.bbox[2] - block.bbox[0])
        desired_width = max(min(_MIN_IMAGE_WIDTH, available_width), min(desired_width, available_width))
        desired_height = desired_width * prepared.height_px / max(prepared.width_px, 1)
        max_height = self.available_height - 10
        if desired_height > max_height:
            scale = max_height / desired_height
            desired_width *= scale
            desired_height *= scale
        image = ReportLabImage(BytesIO(prepared.data), width=desired_width, height=desired_height)
        image.hAlign = "LEFT"
        image.spaceBefore = 5
        image.spaceAfter = 5
        return image

    def _placeholder(
        self,
        label: str,
        *,
        block: BlockBase,
        page_idx: int,
        width: float | None = None,
        url: str | None = None,
    ) -> Table:
        """Creates a stable placeholder box with a light border, optional remote link, and positioned text."""
        self._diagnostic("pdf_content_placeholder", label, page_idx, block)
        normalized = re.sub(r"\s+", " ", label).strip()[:_MAX_PLACEHOLDER_TEXT] or "content unavailable"
        markup = render_plain_text_markup(normalized)
        if url:
            markup += f'<br/><a href="{html.escape(url, quote=True)}" color="#0b6fc2">{html.escape(url)}</a>'
        location = self._location(page_idx, block)
        markup += f'<br/><font size="7" color="#6b7280">{html.escape(location)}</font>'
        paragraph = Paragraph(markup, self.styles.placeholder)
        target_width = self.available_width if width is None else max(1.0, min(width, self.available_width))
        table = Table([[paragraph]], colWidths=[target_width], rowHeights=[_PLACEHOLDER_HEIGHT], hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), SURFACE_COLOR),
                    ("BOX", (0, 0), (-1, -1), 0.6, BORDER_COLOR),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        table.spaceBefore = 5
        table.spaceAfter = 5
        return table

    @staticmethod
    def _location(page_idx: int, block: BlockBase) -> str:
        """Returns stable page/block positioning for PDF diagnostics and placeholder usage."""
        return f"page_idx={page_idx}, block_index={block.index}, block_type={block.type}"

    def _diagnostic(self, code: str, message: str, page_idx: int, block: BlockBase) -> None:
        """Simultaneously report DEBUG logs and structured PDF diagnostics in a unified location format."""
        report_pdf_diagnostic(code, f"{message} ({self._location(page_idx, block)})", page_idx)


def render_pdf(
    middle_json: MiddleJson,
    *,
    asset_resolver: AssetResolver | None = None,
    document_title: str | None = None,
    layout: PdfLayout = PdfLayout.AUTO,
) -> bytes:
    """Render strict MiddleJson to full PDF bytes without side effects."""
    if not isinstance(middle_json, MiddleJson):
        raise TypeError("render_pdf expects a MiddleJson instance")
    if asset_resolver is not None and not callable(asset_resolver):
        raise TypeError("asset_resolver must be callable or None")
    if document_title is not None and not isinstance(document_title, str):
        raise TypeError("document_title must be a string or None")
    if not isinstance(layout, PdfLayout):
        raise TypeError("layout must be a PdfLayout value")
    if layout is PdfLayout.ORIGINAL and middle_json.metadata.file_suffix != "pdf":
        raise ValueError(f"Original PDF layout does not support source format: {middle_json.metadata.file_suffix}")
    if layout is not PdfLayout.REFLOW and middle_json.metadata.file_suffix == "pdf":
        from ....document.pdf.layout import read_layout_geometry
        from .original import OriginalPdfRenderer

        try:
            page_sizes = read_layout_geometry(middle_json)
        except ValueError as exc:
            if layout is PdfLayout.ORIGINAL:
                raise
            report_pdf_diagnostic("pdf_layout_reflow_fallback", str(exc))
        else:
            return OriginalPdfRenderer(
                middle_json,
                asset_resolver=asset_resolver,
                document_title=document_title,
                page_sizes=page_sizes,
            ).render()
    return _PdfRenderer(
        middle_json,
        asset_resolver=asset_resolver,
        document_title=document_title,
    ).render()


def _resolve_document_title(middle_json: MiddleJson, explicit: str | None) -> str:
    """Generates metadata title in the order of explicit title, first document title, and fixed fallback."""
    if explicit is not None:
        normalized = re.sub(r"\s+", " ", _INVALID_METADATA_TEXT_RE.sub("\ufffd", explicit)).strip()
        return normalized or "DocVortex Document"
    for page in middle_json.pages:
        for block in page.blocks:
            if isinstance(block, DocTitleBlock):
                normalized = re.sub(
                    r"\s+",
                    " ",
                    _INVALID_METADATA_TEXT_RE.sub("\ufffd", inline_plain_text(block.content)),
                ).strip()
                if normalized:
                    return normalized
    return "DocVortex Document"


def _iter_document_anchors(middle_json: MiddleJson) -> Iterable[str]:
    """Non-null anchor that enumerates body, title, and page footer in document order."""
    for page in middle_json.pages:
        for block in page.blocks:
            if isinstance(block, (TextBlock, TitleBlockBase)) and block.anchor:
                yield block.anchor
            elif isinstance(block, PageFootnoteBlock) and block.anchor:
                yield block.anchor


def _plain_html_text(content: str) -> str:
    """Condenses the visual body's HTML or plain string into visible text."""
    if not content:
        return ""
    soup = BeautifulSoup(content, "html.parser")
    if soup.find(_VISIBLE_HTML_TAGS) is None:
        return re.sub(r"[ \t]+", " ", content).strip()
    return re.sub(r"[ \t]+", " ", soup.get_text("\n")).strip()


def _flatten_non_link_spans(spans: list[InlineSpan]) -> list[NonLinkInlineSpan]:
    """Recursively remove existing hyperlink wrappers for directory targets to establish single-level internal linking."""
    flattened: list[NonLinkInlineSpan] = []
    for span in spans:
        if isinstance(span, HyperlinkSpan):
            flattened.extend(span.content)
        else:
            flattened.append(span)
    return flattened


def _has_image_payload(block: ImagePayloadBlock) -> bool:
    """Determine whether the unified image payload declares sidecar, data URI or remote URL."""
    return block.image_path is not None or block.image_base64 is not None or block.image_url is not None


def _is_remote_url(source: str) -> bool:
    """Determine whether the picture source is HTTP (S) URL which will not be downloaded by PDF renderer."""
    normalized = source.strip().casefold()
    return normalized.startswith("http://") or normalized.startswith("https://")


__all__ = ["render_pdf"]
