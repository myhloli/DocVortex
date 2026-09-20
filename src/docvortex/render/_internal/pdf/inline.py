"""Middle JSON InlineSpan 到 ReportLab Paragraph fragment 的转换。"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha1
import html
import re
from typing import Iterable

from reportlab.lib.abag import ABag
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph
from reportlab.platypus.paraparser import ParaParser

from ....schema import CodeInlineSpan, EquationInlineSpan, HyperlinkSpan, InlineSpan, TextSpan
from .formula import FormulaRenderer, InlineFormulaImage, PdfFormulaError
from .diagnostics import report_pdf_diagnostic
from .paragraph import CJKParagraph, MeasuredCJKParagraph, MeasuredParagraph, PlainCJKParagraph
from .styles import ACCENT_COLOR, HAN_FONT, JAPANESE_FONT, KOREAN_FONT, MONO_FONT, UNICODE_FALLBACK_FONT

_BOOKMARK_SAFE_RE = re.compile(r"[^A-Za-z0-9_]+")
_BOOKMARK_MAX_LENGTH = 80
_FORMULA_SOURCE_PREFIX = "docvortex-formula:"
_INVALID_TEXT_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


class PdfAnchorRegistry:
    """把 MiddleJson anchor 映射为稳定唯一的 PDF destination 名称。"""

    def __init__(self, anchors: Iterable[str]) -> None:
        """预注册全部可见标题与页面脚注 anchor。"""
        self._names: dict[str, str] = {}
        self._attached: set[str] = set()
        self._used_names: set[str] = set()
        for anchor in anchors:
            normalized = anchor.strip()
            if normalized and normalized not in self._names:
                self._names[normalized] = self._allocate_name(normalized)

    def resolve(self, anchor: str | None) -> str | None:
        """解析已注册 anchor，空值或未知值返回 None。"""
        normalized = (anchor or "").strip()
        return self._names.get(normalized) if normalized else None

    def attach_markup(self, anchor: str | None) -> str:
        """返回首个目标的 Paragraph anchor 标签，重复目标仅记录诊断。"""
        normalized = (anchor or "").strip()
        name = self.resolve(normalized)
        if name is None:
            return ""
        if normalized in self._attached:
            report_pdf_diagnostic("pdf_duplicate_anchor", f"Duplicate PDF anchor ignored: {normalized}")
            return ""
        self._attached.add(normalized)
        return f'<a name="{html.escape(name, quote=True)}"/>'

    def _allocate_name(self, anchor: str) -> str:
        """为原始 anchor 分配合法 ASCII destination，并稳定处理碰撞。"""
        base = _BOOKMARK_SAFE_RE.sub("_", anchor).strip("_")
        if not base or not base[0].isalpha():
            base = f"b_{base}"
        candidate = base[:_BOOKMARK_MAX_LENGTH]
        if candidate not in self._used_names:
            self._used_names.add(candidate)
            return candidate
        digest = sha1(anchor.encode("utf-8")).hexdigest()[:10]
        prefix_length = _BOOKMARK_MAX_LENGTH - len(digest) - 1
        candidate = f"{base[:prefix_length]}_{digest}"
        ordinal = 1
        while candidate in self._used_names:
            suffix = f"_{ordinal}"
            candidate = f"{candidate[: _BOOKMARK_MAX_LENGTH - len(suffix)]}{suffix}"
            ordinal += 1
        self._used_names.add(candidate)
        return candidate


@dataclass(slots=True)
class PdfInlineContext:
    """保存一份 PDF 文档共享的公式、anchor 与定位状态。"""

    formulas: FormulaRenderer
    anchors: PdfAnchorRegistry
    formula_images: dict[str, InlineFormulaImage] = field(default_factory=dict)
    next_formula_id: int = 0
    cache_paragraphs: bool = False

    def location(self, page_idx: int, block_index: int | None, block_type: str) -> str:
        """返回稳定的 page/block 诊断定位文本。"""
        return f"page_idx={page_idx}, block_index={block_index}, block_type={block_type}"

    def register_formula(self, image: InlineFormulaImage) -> str:
        """注册一个 Paragraph 可引用的行内公式代理并返回内部 token。"""
        token = f"{_FORMULA_SOURCE_PREFIX}{self.next_formula_id}"
        self.next_formula_id += 1
        self.formula_images[token] = image
        return token


class _PdfParaParser(ParaParser):
    """让 ReportLab Paragraph 接受不经 ImageReader 的行内公式 token。"""

    def __init__(self, formula_images: dict[str, InlineFormulaImage]) -> None:
        """保存当前文档的公式代理映射。"""
        super().__init__()
        self._formula_images = formula_images

    def end_img(self) -> None:
        """把 docvortex-formula token 解析为带基线几何的矢量图片 fragment。"""
        frag = self._stack[-1]
        if not getattr(frag, "_selfClosingTag", ""):
            raise ValueError("Parser failure in <img/>")
        source = str(getattr(frag, "src", ""))
        image = self._formula_images.get(source)
        if image is None:
            raise ValueError(f"Unknown PDF inline formula token: {source}")
        vector = image.vector
        definition = frag.cbDefn = ABag()
        definition.kind = "img"
        definition.src = source
        definition.image = image
        definition.width = vector.width
        definition.height = vector.height
        definition.valign = -vector.descent
        del frag._selfClosingTag
        self.handle_data("")
        self._pop("img")


def build_pdf_paragraph(
    spans: list[InlineSpan],
    style: ParagraphStyle,
    *,
    context: PdfInlineContext,
    page_idx: int,
    block_index: int | None,
    block_type: str,
    max_width: float,
    anchor: str | None = None,
    preserve_newlines: bool = False,
) -> Paragraph:
    """把完整 InlineSpan 列表构造成支持矢量公式与链接的 Paragraph。"""
    markup = context.anchors.attach_markup(anchor)
    markup += render_pdf_inline_markup(
        spans,
        context=context,
        page_idx=page_idx,
        block_index=block_index,
        block_type=block_type,
        font_size=style.fontSize,
        max_width=max_width,
        preserve_newlines=preserve_newlines,
    )
    parser = _PdfParaParser(context.formula_images)
    parsed_style, fragments, bullet_fragments = parser.parse(markup or "&#8203;", style)
    # 中文及混排正文按字符边界断行，避免空格间的一长串中文被当作英文单词整体移行。
    # 只复制当前段落样式；代码的字面换行及共享样式不受影响，富文本与链接仍复用已解析片段。
    if block_type not in ("code_body", "algorithm_body") and any(
        getattr(fragment, "text", "") and fragment.fontName in (HAN_FONT, JAPANESE_FONT, KOREAN_FONT) for fragment in fragments
    ):
        parsed_style = parsed_style.clone(parsed_style.name + " CJK", wordWrap="CJK")
    if parsed_style.wordWrap == "CJK" and context.formula_images:
        for fragment in fragments:
            if isinstance(getattr(getattr(fragment, "cbDefn", None), "image", None), InlineFormulaImage):
                fragment._pdf_location = context.location(page_idx, block_index, block_type)
                fragment._pdf_page_idx = page_idx
                # 公式的上下界参与实际行高，避免高分数与前后行重叠；不修改共享样式。
                if getattr(parsed_style, "autoLeading", "") in ("", "off"):
                    parsed_style = parsed_style.clone(parsed_style.name + " Formula", autoLeading="max")
    bullet_text = None
    if bullet_fragments:
        bullet_text = "".join(getattr(fragment, "text", "") for fragment in bullet_fragments)
    # 短 Latin 单元格原生换行很便宜，状态快照反而更慢；只缓存 CJK 或较长的普通段落。
    cache_measurement = (
        context.cache_paragraphs
        and not getattr(style, "keepWithNext", False)
        and (parsed_style.wordWrap == "CJK" or sum(len(getattr(fragment, "text", "")) for fragment in fragments) >= 256)
    )
    paragraph_class = (
        MeasuredParagraph if cache_measurement else (CJKParagraph if parsed_style.wordWrap == "CJK" else Paragraph)
    )
    if (
        parsed_style.wordWrap == "CJK"
        and not anchor
        and not getattr(style, "keepWithNext", False)
        and block_type not in ("code_body", "algorithm_body")
        and all(isinstance(span, TextSpan) and not span.styles for span in spans)
    ):
        paragraph_class = MeasuredCJKParagraph if cache_measurement else PlainCJKParagraph
    return paragraph_class("", parsed_style, bulletText=bullet_text, frags=fragments)


def render_pdf_inline_markup(
    spans: list[InlineSpan],
    *,
    context: PdfInlineContext,
    page_idx: int,
    block_index: int | None,
    block_type: str,
    font_size: float,
    max_width: float,
    preserve_newlines: bool = False,
) -> str:
    """把行内 Span 转成仅包含 renderer 生成标签的安全 Paragraph markup。"""
    return "".join(
        _render_span(
            span,
            context=context,
            page_idx=page_idx,
            block_index=block_index,
            block_type=block_type,
            font_size=font_size,
            max_width=max_width,
            preserve_newlines=preserve_newlines,
        )
        for span in spans
    )


def render_plain_text_markup(text: str, *, preserve_newlines: bool = False) -> str:
    """转义普通文本，并按字符脚本显式选择 Latin 或中日韩字体。"""
    normalized = _INVALID_TEXT_RE.sub("\ufffd", text)
    if not normalized:
        return ""
    segments: list[str] = []
    current_font: str | None = None
    current_text: list[str] = []

    def flush() -> None:
        """把当前同字体字符片段编码为 Paragraph markup。"""
        if not current_text:
            return
        escaped = html.escape("".join(current_text), quote=False)
        if preserve_newlines:
            escaped = escaped.replace("\n", "<br/>")
        else:
            escaped = escaped.replace("\n", " ")
        if current_font is None:
            segments.append(escaped)
        else:
            segments.append(f'<font name="{current_font}">{escaped}</font>')
        current_text.clear()

    for character in normalized:
        font = _font_for_character(character)
        if font != current_font:
            flush()
            current_font = font
        current_text.append(character)
    flush()
    return "".join(segments)


def _render_span(
    span: InlineSpan,
    *,
    context: PdfInlineContext,
    page_idx: int,
    block_index: int | None,
    block_type: str,
    font_size: float,
    max_width: float,
    preserve_newlines: bool,
) -> str:
    """按 InlineSpan discriminator 输出单个安全 Paragraph 片段。"""
    if isinstance(span, TextSpan):
        markup = render_plain_text_markup(span.content, preserve_newlines=preserve_newlines)
        return _apply_text_styles(markup, tuple(span.styles))
    if isinstance(span, CodeInlineSpan):
        markup = render_plain_text_markup(span.content, preserve_newlines=preserve_newlines)
        return f'<font name="{MONO_FONT}" size="{max(6.0, font_size * 0.9):.3f}">{markup}</font>'
    if isinstance(span, EquationInlineSpan):
        try:
            vector = context.formulas.render(span.content, inline=True, font_size=font_size)
            if vector.width > max_width > 0:
                vector = vector.scaled(max_width / vector.width)
            token = context.register_formula(InlineFormulaImage(vector))
            return (
                f'<img src="{token}" width="{vector.width:.3f}" height="{vector.height:.3f}" valign="{-vector.descent:.3f}"/>'
            )
        except PdfFormulaError as exc:
            report_pdf_diagnostic(
                "pdf_formula_fallback",
                f"PDF inline formula fallback: {exc} ({context.location(page_idx, block_index, block_type)})",
                page_idx,
            )
            fallback = render_plain_text_markup(f"${span.content}$", preserve_newlines=preserve_newlines)
            return f'<font name="{MONO_FONT}" color="#6b7280">{fallback}</font>'
    if isinstance(span, HyperlinkSpan):
        child_markup = "".join(
            _render_span(
                child,
                context=context,
                page_idx=page_idx,
                block_index=block_index,
                block_type=block_type,
                font_size=font_size,
                max_width=max_width,
                preserve_newlines=preserve_newlines,
            )
            for child in span.content
        )
        target = _resolve_link_target(span.url, context.anchors)
        if target is None:
            if span.url.startswith("#"):
                report_pdf_diagnostic(
                    "pdf_unmatched_link",
                    f"Unmatched PDF internal link: {span.url} ({context.location(page_idx, block_index, block_type)})",
                    page_idx,
                )
            return child_markup
        return f'<a href="{html.escape(target, quote=True)}" color="{ACCENT_COLOR.hexval()}" underline="1">{child_markup}</a>'
    raise TypeError(f"Unsupported InlineSpan type: {type(span).__name__}")


def _apply_text_styles(markup: str, styles: tuple[str, ...]) -> str:
    """按固定顺序把 MiddleJson 文本样式映射到 Paragraph 标签。"""
    if not markup:
        return ""
    tags: list[str] = []
    if "bold" in styles:
        tags.append("b")
    if "italic" in styles:
        tags.append("i")
    if "underline" in styles or "emphasis" in styles:
        tags.append("u")
    if "strikethrough" in styles:
        tags.append("strike")
    if "superscript" in styles:
        tags.append("super")
    elif "subscript" in styles:
        tags.append("sub")
    for tag in tags:
        markup = f"<{tag}>{markup}</{tag}>"
    return markup


def _resolve_link_target(url: str, anchors: PdfAnchorRegistry) -> str | None:
    """把 fragment 映射为 PDF destination，其他安全 URL 原样保留。"""
    if url.startswith("#"):
        destination = anchors.resolve(url[1:])
        return f"#{destination}" if destination is not None else None
    return url or None


def _font_for_character(character: str) -> str | None:
    """为中日韩字符选择 CID 字体，并为非 WinAnsi 字符选择 Unicode 回退。"""
    # 标准字体把 bullet 映射到 0x7f，部分提取器会复制成控制字符；嵌入字体保留 ToUnicode。
    if character == "•":
        return UNICODE_FALLBACK_FONT
    codepoint = ord(character)
    if 0x3040 <= codepoint <= 0x30FF or 0x31F0 <= codepoint <= 0x31FF or 0xFF66 <= codepoint <= 0xFF9D:
        return JAPANESE_FONT
    if 0x1100 <= codepoint <= 0x11FF or 0x3130 <= codepoint <= 0x318F or 0xAC00 <= codepoint <= 0xD7AF:
        return KOREAN_FONT
    if (
        0x2E80 <= codepoint <= 0x303F
        or 0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0xFF00 <= codepoint <= 0xFF65
        or 0x20000 <= codepoint <= 0x2FA1F
    ):
        return HAN_FONT
    try:
        character.encode("cp1252")
    except UnicodeEncodeError:
        return UNICODE_FALLBACK_FONT
    return None


__all__ = [
    "PdfAnchorRegistry",
    "PdfInlineContext",
    "build_pdf_paragraph",
    "render_pdf_inline_markup",
    "render_plain_text_markup",
]
