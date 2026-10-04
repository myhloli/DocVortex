"""Markdown block level serialization common to Content List."""

from __future__ import annotations

import html
import re

from ....options import LatexDelimitersConfig
from ..common.index import strip_index_page_tail
from ..common.list_items import has_markdown_unordered_marker, reference_list_needs_bullets
from ..common.planner import PlannedBlock
from .assets import build_markdown_image, resolve_image_source
from .escaping import escape_standalone_marker_rule, escape_text_block_markdown_prefix
from .inline import (
    render_inline_content,
    render_inline_spans,
    render_inline_spans_in_html_context,
    render_internal_link,
    render_joined_inline_contents,
)
from .table import _strip_embedded_images, format_embedded_html, render_html_table
from ...contracts import ImageRenderer
from ....schema import (
    RAW_ALGORITHM,
    AlgorithmBodyBlock,
    BlockType,
    ChartAnnotationBlock,
    ChartBlock,
    ChartBodyBlock,
    CodeAnnotationBlock,
    CodeBlock,
    CodeBodyBlock,
    DocTitleBlock,
    EquationBlock,
    ImageAnnotationBlock,
    ImageBlock,
    ImageBodyBlock,
    InlineSpan,
    IndexBlock,
    ListBlock,
    PageAuxTextBlock,
    PageFootnoteBlock,
    PageBlock,
    ParagraphTitleBlock,
    RefTextBlock,
    TableAnnotationBlock,
    TableBlock,
    TableBodyBlock,
    TextBlock,
)

_VALID_CODE_LANGUAGE_RE = re.compile(r"[A-Za-z0-9_.+#-]+")


def render_planned_block(
    planned: PlannedBlock,
    *,
    delimiters: LatexDelimitersConfig,
    asset_base_url: str,
    image_renderer: ImageRenderer | None = None,
    anchor_targets: set[str] | None = None,
    emitted_anchors: set[str] | None = None,
) -> str:
    """Distributes Markdown renderings by specific Pydantic block type."""
    block = planned.block
    if isinstance(block, TextBlock):
        content = render_joined_inline_contents(planned.text_contents or [block.content], delimiters)
        rendered = escape_standalone_marker_rule(escape_text_block_markdown_prefix(content))
        return _prepend_markdown_anchor(rendered, block.anchor, emitted_anchors)
    if isinstance(block, RefTextBlock):
        content = render_joined_inline_contents(planned.text_contents or [block.content], delimiters)
        return escape_standalone_marker_rule(content)
    if isinstance(block, (DocTitleBlock, ParagraphTitleBlock)):
        return _render_title(block, delimiters, emitted_anchors)
    if isinstance(block, PageFootnoteBlock):
        return _render_page_footnote(block, delimiters, emitted_anchors)
    if isinstance(block, PageAuxTextBlock):
        content = escape_text_block_markdown_prefix(render_inline_content(block.content, delimiters))
        return escape_standalone_marker_rule(content)
    if isinstance(block, EquationBlock):
        return _render_equation(block, delimiters, asset_base_url, image_renderer)
    if isinstance(block, ListBlock):
        return _render_list(block, delimiters)
    if isinstance(block, IndexBlock):
        return _render_index(block, delimiters, anchor_targets=anchor_targets)
    if isinstance(block, ImageBlock):
        return _render_image_block(block, delimiters, asset_base_url, image_renderer)
    if isinstance(block, TableBlock):
        return _render_table_block(block, delimiters, asset_base_url, image_renderer)
    if isinstance(block, ChartBlock):
        return _render_chart_block(block, delimiters, asset_base_url, image_renderer)
    if isinstance(block, CodeBlock):
        return _render_code_block(block, delimiters)
    raise TypeError(f"Unsupported PageBlock type: {type(block).__name__}")


def _claim_markdown_anchor(anchor: str | None, emitted_anchors: set[str] | None) -> str:
    """Normalize the Markdown target and register only the first occurrence of anchor in the document-level collection."""
    normalized = (anchor or "").strip()
    if not normalized:
        return ""
    if emitted_anchors is not None:
        if normalized in emitted_anchors:
            return ""
        emitted_anchors.add(normalized)
    return normalized


def _prepend_markdown_anchor(content: str, anchor: str | None, emitted_anchors: set[str] | None) -> str:
    """Add independent HTML anchor to the non-empty Markdown body to avoid contaminating the body text."""
    if not content.strip():
        return content
    normalized = _claim_markdown_anchor(anchor, emitted_anchors)
    if not normalized:
        return content
    return f'<a id="{html.escape(normalized, quote=True)}"></a>\n{content}'


def _render_page_footnote(
    block: PageFootnoteBlock,
    delimiters: LatexDelimitersConfig,
    emitted_anchors: set[str] | None = None,
) -> str:
    """Output page footers are identified with HTML, a small, non-folded, light-colored font."""
    content = escape_text_block_markdown_prefix(render_inline_content(block.content, delimiters))
    rendered = escape_standalone_marker_rule(content)
    if not rendered.strip():
        return ""
    rendered = rendered.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
    attrs = [
        'class="docvortex-page-footnote"',
        'data-block-type="page_footnote"',
        'style="color:#6b7280"',
    ]
    anchor = _claim_markdown_anchor(block.anchor, emitted_anchors)
    if anchor:
        attrs.insert(0, f'id="{html.escape(anchor, quote=True)}"')
    return f"<small><span {' '.join(attrs)}>{rendered}</span></small>"


def render_single_block(
    block: PageBlock,
    *,
    delimiters: LatexDelimitersConfig,
    asset_base_url: str,
    image_renderer: ImageRenderer | None = None,
) -> str:
    """No continuation merging or page filtering is performed, and a top-level block is rendered directly."""
    text_contents = [block.content] if isinstance(block, (TextBlock, RefTextBlock)) else []
    planned = PlannedBlock(page_idx=0, block=block, text_contents=text_contents)
    return render_planned_block(
        planned,
        delimiters=delimiters,
        asset_base_url=asset_base_url,
        image_renderer=image_renderer,
    )


def _render_title(
    block: DocTitleBlock | ParagraphTitleBlock,
    delimiters: LatexDelimitersConfig,
    emitted_anchors: set[str] | None = None,
) -> str:
    """Renders Markdown title with optional HTML anchor."""
    level = min(max(block.level, 1), 6)
    title = f"{'#' * level} {render_title_inline_content(block, delimiters)}"
    anchor = _claim_markdown_anchor(block.anchor, emitted_anchors)
    if not anchor:
        return title
    return f'<a id="{html.escape(anchor, quote=True)}"></a>\n{title}'


def render_title_inline_content(
    block: DocTitleBlock | ParagraphTitleBlock,
    delimiters: LatexDelimitersConfig,
) -> str:
    """Only the inline semantics of the title are rendered, without adding the heading tag or HTML anchor."""
    return render_inline_content(block.content, delimiters)


def _render_equation(
    block: EquationBlock,
    delimiters: LatexDelimitersConfig,
    asset_base_url: str,
    image_renderer: ImageRenderer | None,
) -> str:
    """Priority is given to rendering LaTeX between lines, and empty formula content falls back to image_renderer or formula pictures."""
    latex = block.content.strip()
    if latex:
        return f"{delimiters.display.left}\n{latex}\n{delimiters.display.right}"
    if image_renderer is not None:
        rendered = image_renderer(block)
        if rendered:
            return rendered
    source = resolve_image_source(block, asset_base_url)
    return build_markdown_image(source) if source else ""


def _render_list(block: ListBlock, delimiters: LatexDelimitersConfig, depth: int = 0) -> str:
    """Render the list recursively and add an out-of-order mark to references where most entries do not have a numeric prefix."""
    add_ref_bullets = reference_list_needs_bullets(block)
    lines: list[str] = []
    for child in block.content:
        if isinstance(child, ListBlock):
            nested = _render_list(child, delimiters, depth + 1)
            if nested:
                lines.extend(nested.splitlines())
            continue
        item = render_inline_content(child.content, delimiters)
        if not item:
            continue
        item = escape_standalone_marker_rule(item)
        indent = "    " * depth
        item_lines = item.splitlines() or [item]
        if add_ref_bullets and not has_markdown_unordered_marker(child.content):
            lines.append(f"{indent}- {item_lines[0]}")
            lines.extend(f"{indent}  {line}" for line in item_lines[1:])
        else:
            lines.extend(f"{indent}{line}" for line in item_lines)
    return "\n".join(lines)


def _render_index(
    block: IndexBlock,
    delimiters: LatexDelimitersConfig,
    depth: int = 0,
    *,
    anchor_targets: set[str] | None = None,
) -> str:
    """Render the directory listing recursively and add an internal anchor link to the title leaf."""
    lines: list[str] = []
    for child in block.content:
        if isinstance(child, IndexBlock):
            nested = _render_index(child, delimiters, depth + 1, anchor_targets=anchor_targets)
            if nested:
                lines.extend(nested.splitlines())
            continue
        content = strip_index_page_tail(child.content)
        label = render_inline_content(content, delimiters).strip()
        if not label:
            continue
        anchor = (child.anchor or "").strip()
        if anchor and (anchor_targets is None or anchor in anchor_targets):
            label = render_internal_link(label, anchor)
        lines.append(f"{'    ' * depth}- {label}")
    return "\n".join(lines)


def _render_image_block(
    block: ImageBlock,
    delimiters: LatexDelimitersConfig,
    asset_base_url: str,
    image_renderer: ImageRenderer | None,
) -> str:
    """Render the image body and description text in original sub-block order."""
    parts: list[str] = []
    for child in block.content:
        if isinstance(child, ImageBodyBlock):
            body = _render_visual_body_child(
                block,
                child,
                delimiters=delimiters,
                asset_base_url=asset_base_url,
                image_renderer=image_renderer,
            )
        elif isinstance(child, ImageAnnotationBlock):
            body = render_visual_annotation(child, delimiters)
        else:
            raise TypeError(f"Unsupported image child: {type(child).__name__}")
        if body:
            parts.append(body)
    return _join_visual_parts(parts)


def _render_chart_block(
    block: ChartBlock,
    delimiters: LatexDelimitersConfig,
    asset_base_url: str,
    image_renderer: ImageRenderer | None,
) -> str:
    """Render diagram images, structural content, and explanatory text in original sub-block order."""
    parts: list[str] = []
    for child in block.content:
        if isinstance(child, ChartBodyBlock):
            body = _render_visual_body_child(
                block,
                child,
                delimiters=delimiters,
                asset_base_url=asset_base_url,
                image_renderer=image_renderer,
            )
        elif isinstance(child, ChartAnnotationBlock):
            body = render_visual_annotation(child, delimiters)
        else:
            raise TypeError(f"Unsupported chart child: {type(child).__name__}")
        if body:
            parts.append(body)
    return _join_visual_parts(parts)


def _render_media_body(
    block: ImageBodyBlock,
    *,
    summary: str,
    delimiters: LatexDelimitersConfig,
    asset_base_url: str,
    image_renderer: ImageRenderer | None,
    parent_block: ImageBlock,
) -> str:
    """Render the image payload and place the recognized content into the collapsed details. image_renderer takes precedence over image_path."""
    rendered_content = _render_media_content(block, delimiters, asset_base_url)
    if image_renderer is not None:
        image_ref = image_renderer(parent_block)
        parts = [image_ref] if image_ref else []
    else:
        source = resolve_image_source(block, asset_base_url)
        parts = [build_markdown_image(source)] if source else []
    if rendered_content:
        parts.append(_render_details(rendered_content, summary))
    return "\n\n".join(part for part in parts if part)


def _render_media_content(
    block: ImageBodyBlock,
    delimiters: LatexDelimitersConfig,
    asset_base_url: str,
) -> str:
    """Render image recognition content without adding image syntax or folding detail packaging."""
    content = block.content.strip()
    if not content:
        return ""
    return format_embedded_html(content, asset_base_url=asset_base_url, delimiters=delimiters)


def _render_chart_body(
    block: ChartBodyBlock,
    *,
    summary: str,
    delimiters: LatexDelimitersConfig,
    asset_base_url: str,
    image_renderer: ImageRenderer | None,
    parent_block: ChartBlock,
) -> str:
    """Render chart images and uniformly convert their HTML table contents. Fallback to image_renderer or picture when content is empty."""
    rendered_content = _render_chart_content(block.content, delimiters, asset_base_url)
    if rendered_content:
        return _render_chart_with_details(rendered_content, block, asset_base_url, summary)
    if image_renderer is not None:
        rendered = image_renderer(parent_block)
        if rendered:
            return rendered
    source = resolve_image_source(block, asset_base_url)
    return build_markdown_image(source) if source else ""


def _render_chart_with_details(
    rendered_content: str,
    block: ChartBodyBlock,
    asset_base_url: str,
    summary: str,
) -> str:
    """chart When there is structured content, attach pictures and folding details."""
    source = resolve_image_source(block, asset_base_url)
    if source:
        parts = [build_markdown_image(source), _render_details(rendered_content, summary)]
        return "\n\n".join(part for part in parts if part)
    return rendered_content


def _render_chart_content(
    content: str,
    delimiters: LatexDelimitersConfig,
    asset_base_url: str,
) -> str:
    """Convert the simple HTML table in the chart content to GFM, and keep the other content as it is."""
    normalized = content.strip()
    if not normalized:
        return ""
    html_table = render_html_table(
        normalized,
        asset_base_url=asset_base_url,
        delimiters=delimiters,
    )
    if html_table is not None:
        return html_table
    return format_embedded_html(normalized, asset_base_url=asset_base_url, delimiters=delimiters)


def _render_details(content: str, summary: str) -> str:
    """Constructs a collapsed HTML detail block that retains rendered visual content."""
    safe_summary = html.escape(summary, quote=False)
    return f"<details>\n<summary>{safe_summary}</summary>\n\n{content.strip()}\n</details>"


def _render_table_block(
    block: TableBlock,
    delimiters: LatexDelimitersConfig,
    asset_base_url: str,
    image_renderer: ImageRenderer | None,
) -> str:
    """Render the table body and description text in original sub-block order."""
    parts: list[str] = []
    for child in block.content:
        if isinstance(child, TableBodyBlock):
            body = _render_visual_body_child(
                block,
                child,
                delimiters=delimiters,
                asset_base_url=asset_base_url,
                image_renderer=image_renderer,
            )
        elif isinstance(child, TableAnnotationBlock):
            body = render_visual_annotation(child, delimiters)
        else:
            raise TypeError(f"Unsupported table child: {type(child).__name__}")
        if body:
            parts.append(body)
    return _join_visual_parts(parts)


def _render_table_body(
    block: TableBodyBlock,
    delimiters: LatexDelimitersConfig,
    asset_base_url: str,
    image_renderer: ImageRenderer | None,
    parent_block: TableBlock,
) -> str:
    """Render the table body according to the priority of HTML, spatially projected text, image_renderer, and picture."""
    rendered_content = _render_table_content(
        block,
        delimiters,
        asset_base_url,
        strip_embedded_images=image_renderer is not None,
    )
    if rendered_content:
        return rendered_content
    if image_renderer is not None:
        rendered = image_renderer(parent_block)
        if rendered:
            return rendered
    source = resolve_image_source(block, asset_base_url)
    return build_markdown_image(source) if source else ""


def _render_table_content(
    block: TableBodyBlock,
    delimiters: LatexDelimitersConfig,
    asset_base_url: str,
    *,
    strip_embedded_images: bool = False,
) -> str:
    """Render table structure content and do not perform image rollback when empty content is used."""
    content = _strip_embedded_images(block.content) if strip_embedded_images else block.content
    if not content:
        return ""
    html_table = render_html_table(
        content,
        asset_base_url=asset_base_url,
        delimiters=delimiters,
    )
    if html_table is not None:
        return html_table
    return _render_fenced_content(content)


def _render_code_block(block: CodeBlock, delimiters: LatexDelimitersConfig) -> str:
    """Render normal code or algorithm supporting formulas by parent block subtype."""
    parts: list[str] = []
    for child in block.content:
        if isinstance(child, (CodeBodyBlock, AlgorithmBodyBlock)):
            body = _render_visual_body_child(
                block,
                child,
                delimiters=delimiters,
                asset_base_url="",
                image_renderer=None,
            )
        elif isinstance(child, CodeAnnotationBlock):
            body = render_visual_annotation(child, delimiters)
        else:
            raise TypeError(f"Unsupported code child: {type(child).__name__}")
        if body:
            parts.append(body)
    return _join_visual_parts(parts)


def render_visual_body_content(
    block: ImageBlock | TableBlock | ChartBlock | CodeBlock,
    *,
    delimiters: LatexDelimitersConfig,
    asset_base_url: str,
) -> str:
    """Finds the visual parent block unique body and only renders semantic content that can be structured for consumption."""
    for child in block.content:
        if isinstance(block, ImageBlock) and isinstance(child, ImageBodyBlock):
            return _render_media_content(child, delimiters, asset_base_url)
        if isinstance(block, TableBlock) and isinstance(child, TableBodyBlock):
            return _render_table_content(child, delimiters, asset_base_url)
        if isinstance(block, ChartBlock) and isinstance(child, ChartBodyBlock):
            return _render_chart_content(child.content, delimiters, asset_base_url)
        if isinstance(block, CodeBlock) and isinstance(child, (CodeBodyBlock, AlgorithmBodyBlock)):
            return _render_code_semantic_content(block, child, delimiters)
    raise ValueError(f"Missing visual body: {block.type}")


def _render_visual_body_child(
    block: ImageBlock | TableBlock | ChartBlock | CodeBlock,
    child: ImageBodyBlock | TableBodyBlock | ChartBodyBlock | CodeBodyBlock | AlgorithmBodyBlock,
    *,
    delimiters: LatexDelimitersConfig,
    asset_base_url: str,
    image_renderer: ImageRenderer | None = None,
) -> str:
    """Renders a body child block by visual parent-child type combination."""
    if isinstance(block, ImageBlock) and isinstance(child, ImageBodyBlock):
        return _render_media_body(
            child,
            summary=block.sub_type or "image content",
            delimiters=delimiters,
            asset_base_url=asset_base_url,
            image_renderer=image_renderer,
            parent_block=block,
        )
    if isinstance(block, TableBlock) and isinstance(child, TableBodyBlock):
        return _render_table_body(child, delimiters, asset_base_url, image_renderer, block)
    if isinstance(block, ChartBlock) and isinstance(child, ChartBodyBlock):
        return _render_chart_body(
            child,
            summary=block.sub_type or "chart content",
            delimiters=delimiters,
            asset_base_url=asset_base_url,
            image_renderer=image_renderer,
            parent_block=block,
        )
    if isinstance(block, CodeBlock) and isinstance(child, (CodeBodyBlock, AlgorithmBodyBlock)):
        return _render_code_body(block, child, delimiters)
    raise TypeError(f"Unsupported visual body pair: {block.type}/{child.type}")


def render_visual_annotation(
    block: ImageAnnotationBlock | TableAnnotationBlock | ChartAnnotationBlock | CodeAnnotationBlock,
    delimiters: LatexDelimitersConfig,
) -> str:
    """Render a visual description subchunk as a separate Markdown string."""
    return escape_standalone_marker_rule(render_inline_content(block.content, delimiters))


def _render_code_body(
    block: CodeBlock,
    child: CodeBodyBlock | AlgorithmBodyBlock,
    delimiters: LatexDelimitersConfig,
) -> str:
    """Rendering code or algorithm body based on parent block subtype."""
    if block.sub_type == RAW_ALGORITHM:
        if not isinstance(child, AlgorithmBodyBlock):
            raise TypeError("algorithm subtype requires AlgorithmBodyBlock")
        return _render_algorithm_html(child.content, delimiters)
    return _render_code_semantic_content(block, child, delimiters)


def _render_code_semantic_content(
    block: CodeBlock,
    child: CodeBodyBlock | AlgorithmBodyBlock,
    delimiters: LatexDelimitersConfig,
) -> str:
    """Render code or algorithm body to Structured Content consumable string of class Markdown."""
    if block.sub_type == BlockType.CODE:
        if not isinstance(child, CodeBodyBlock):
            raise TypeError("code subtype requires CodeBodyBlock")
        return _render_fenced_content(child.content, _normalize_code_language(block.guess_lang))
    if block.sub_type == RAW_ALGORITHM:
        if not isinstance(child, AlgorithmBodyBlock):
            raise TypeError("algorithm subtype requires AlgorithmBodyBlock")
        return _render_algorithm_markdown(child.content, delimiters)
    raise ValueError(f"Unsupported code subtype: {block.sub_type}")


def _normalize_code_language(language: str | None) -> str:
    """Verification fenced code info string, illegal values fall back to txt."""
    normalized = (language or "").strip()
    if not normalized or _VALID_CODE_LANGUAGE_RE.fullmatch(normalized) is None:
        return "txt"
    return normalized


def _render_fenced_content(content: str, language: str | None = None) -> str:
    """Wrap the original content with a fence longer than the body backtick run."""
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", content)), default=0)
    fence = "`" * max(3, longest + 1)
    opening = f"{fence}{language or ''}"
    closing_prefix = "" if content.endswith("\n") else "\n"
    return f"{opening}\n{content}{closing_prefix}{fence}"


def _render_algorithm_html(content: list[InlineSpan], delimiters: LatexDelimitersConfig) -> str:
    """Refer to dev to implement the algorithm for rendering preserving whitespace, superscripts and subscripts, and inline formulas HTML."""
    parts: list[str] = []
    previous_equation = False
    for span in content:
        current_equation = str(span.type) == "equation_inline"
        if previous_equation and current_equation:
            parts.append(" ")
        parts.append(render_inline_spans_in_html_context([span], delimiters))
        previous_equation = current_equation
    body = "".join(parts)
    if not body.strip():
        return ""
    return f'<div class="docvortex-algorithm" style="white-space: pre-wrap; font-family:monospace;">\n{body}\n</div>'


def _render_algorithm_markdown(content: list[InlineSpan], delimiters: LatexDelimitersConfig) -> str:
    """Renders the algorithm Span as content of class Markdown and separates formulas in adjacent lines."""
    parts: list[str] = []
    previous_equation = False
    for span in content:
        current_equation = str(span.type) == "equation_inline"
        if previous_equation and current_equation:
            parts.append(" ")
        parts.append(render_inline_spans([span], delimiters))
        previous_equation = current_equation
    return "".join(parts)


def _join_visual_parts(parts: list[str]) -> str:
    """Use safe blank lines to connect ordered subblocks within the same visual parent block."""
    return "\n\n".join(part.strip("\n") for part in parts if part and part.strip())


__all__ = [
    "render_planned_block",
    "render_single_block",
    "render_title_inline_content",
    "render_visual_annotation",
    "render_visual_body_content",
]
