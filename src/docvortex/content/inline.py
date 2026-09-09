"""Middle JSON 2.0 行内 Span 的规范化、可见文本与段落边界操作。"""

from __future__ import annotations

from copy import deepcopy
from typing import Callable, Iterable

from ..foundation._text import resolve_text_line_boundary
from ..foundation.language import remove_invalid_surrogates
from ..schema import CodeInlineSpan, EquationInlineSpan, HyperlinkSpan, InlineSpan, TextSpan, parse_inline_spans


def normalize_inline_spans(spans: Iterable[InlineSpan | dict[str, object]]) -> list[InlineSpan]:
    """严格解析、递归规范化并合并相邻等样式文字 Span。"""
    parsed = parse_inline_spans(list(spans))
    normalized: list[InlineSpan] = []
    for span in parsed:
        current: InlineSpan
        if isinstance(span, HyperlinkSpan):
            children = normalize_inline_spans(span.content)
            non_link_children = [child for child in children if not isinstance(child, HyperlinkSpan)]
            if not non_link_children:
                continue
            current = span.model_copy(update={"content": non_link_children})
        else:
            current = span.model_copy(deep=True)
        if (
            normalized
            and isinstance(normalized[-1], TextSpan)
            and isinstance(current, TextSpan)
            and normalized[-1].styles == current.styles
        ):
            normalized[-1].content += current.content
            continue
        if (
            normalized
            and isinstance(normalized[-1], HyperlinkSpan)
            and isinstance(current, HyperlinkSpan)
            and normalized[-1].url == current.url
        ):
            merged_children = normalize_inline_spans([*normalized[-1].content, *current.content])
            normalized[-1].content = [child for child in merged_children if not isinstance(child, HyperlinkSpan)]
            continue
        normalized.append(current)
    return normalized


def inline_plain_text(spans: Iterable[InlineSpan]) -> str:
    """提取 Span 列表的完整可见文字，供排序、合并和标题判断使用。"""
    parts: list[str] = []
    for span in spans:
        if isinstance(span, (TextSpan, CodeInlineSpan, EquationInlineSpan)):
            parts.append(span.content)
        elif isinstance(span, HyperlinkSpan):
            parts.append(inline_plain_text(span.content))
    return "".join(parts)


def join_inline_spans(contents: Iterable[Iterable[InlineSpan]]) -> list[InlineSpan]:
    """按物理段落边界规则合并多组 Span，并保持结构化语义。"""
    merged: list[InlineSpan] = []
    for content in contents:
        current = map_text_span_content(list(content), remove_invalid_surrogates)
        if not current:
            continue
        # 边界裁剪最多清空最后一个根节点；保留其前驱以重新合并新相邻的 Span。
        boundary_start = max(0, len(merged) - 2)
        if merged:
            _join_inline_span_sequences(merged, current)
        merged.extend(current)
        merged[boundary_start:] = normalize_inline_spans(_drop_empty_text_spans(merged[boundary_start:]))
    return merged


def strip_inline_spans(spans: Iterable[InlineSpan]) -> list[InlineSpan]:
    """删除行内内容首尾空白，同时保留内部 Span 边界和样式。"""
    normalized = normalize_inline_spans(deepcopy(list(spans)))
    first = _first_text_span(normalized)
    last = _last_text_span(normalized)
    if first is not None:
        object.__setattr__(first, "content", first.content.lstrip())
    if last is not None:
        object.__setattr__(last, "content", last.content.rstrip())
    return _drop_empty_text_spans(normalized)


def replace_inline_text(spans: Iterable[InlineSpan], content: str) -> list[InlineSpan]:
    """把纯文本回填为单一 TextSpan，供确实丢弃原样式的规则使用。"""
    if not content:
        return []
    return [TextSpan(type="text", content=content)]


def slice_inline_spans(spans: Iterable[InlineSpan], start: int = 0, end: int | None = None) -> list[InlineSpan]:
    """按可见字符偏移裁剪 Span，并保留覆盖范围内的样式和链接。"""
    normalized = normalize_inline_spans(deepcopy(list(spans)))
    visible_length = len(inline_plain_text(normalized))
    resolved_start = min(max(start, 0), visible_length)
    resolved_end = visible_length if end is None else min(max(end, resolved_start), visible_length)
    output: list[InlineSpan] = []
    cursor = 0
    for span in normalized:
        span_length = len(inline_plain_text([span]))
        span_end = cursor + span_length
        overlap_start = max(resolved_start, cursor)
        overlap_end = min(resolved_end, span_end)
        if overlap_start < overlap_end:
            local_start = overlap_start - cursor
            local_end = overlap_end - cursor
            sliced = _slice_inline_span(span, local_start, local_end)
            if sliced is not None:
                output.append(sliced)
        cursor = span_end
        if cursor >= resolved_end:
            break
    return normalize_inline_spans(output)


def map_text_span_content(spans: Iterable[InlineSpan], transform: Callable[[str], str]) -> list[InlineSpan]:
    """递归转换 TextSpan 正文，同时保留其它 Span 语义。"""
    output: list[InlineSpan] = []
    for span in normalize_inline_spans(deepcopy(list(spans))):
        if isinstance(span, TextSpan):
            content = transform(span.content)
            if content:
                output.append(span.model_copy(update={"content": content}))
        elif isinstance(span, HyperlinkSpan):
            children = map_text_span_content(span.content, transform)
            non_link_children = [child for child in children if not isinstance(child, HyperlinkSpan)]
            if non_link_children:
                output.append(span.model_copy(update={"content": non_link_children}))
        else:
            output.append(span)
    return normalize_inline_spans(output)


def _join_inline_span_sequences(previous: list[InlineSpan], current: list[InlineSpan]) -> None:
    """按两组 Span 的可见边界拼接，只修改可写文本叶子，保留公式和代码。"""
    previous_visible = inline_plain_text(previous).rstrip()
    current_visible = inline_plain_text(current).lstrip()
    if not previous_visible or not current_visible:
        return

    last_text = _last_text_span(previous)
    first_text = _first_text_span(current)
    if last_text is not None:
        object.__setattr__(last_text, "content", last_text.content.rstrip())
    if first_text is not None:
        object.__setattr__(first_text, "content", first_text.content.lstrip())

    processed, separator = resolve_text_line_boundary(previous_visible, next_content=current_visible)
    if last_text is not None:
        # 共享规则最多删除一个行末断词符；不能把整个可见投影写回单一叶子。
        if processed == previous_visible[:-1] and last_text.content.endswith(previous_visible[-1]):
            object.__setattr__(last_text, "content", last_text.content[:-1])
    if separator:
        previous.append(TextSpan(type="text", content=separator))


def _first_text_span(spans: list[InlineSpan]) -> TextSpan | None:
    """返回首个可见叶子为文字时对应的 TextSpan。"""
    for span in spans:
        if not inline_plain_text([span]):
            continue
        if isinstance(span, TextSpan):
            return span
        if isinstance(span, HyperlinkSpan):
            return _first_text_span(list(span.content))
        return None
    return None


def _last_text_span(spans: list[InlineSpan]) -> TextSpan | None:
    """返回末个可见叶子为文字时对应的 TextSpan。"""
    for span in reversed(spans):
        if not inline_plain_text([span]):
            continue
        if isinstance(span, TextSpan):
            return span
        if isinstance(span, HyperlinkSpan):
            return _last_text_span(list(span.content))
        return None
    return None


def _drop_empty_text_spans(spans: list[InlineSpan]) -> list[InlineSpan]:
    """删除裁剪后为空的 TextSpan，并递归清理空链接。"""
    result: list[InlineSpan] = []
    for span in spans:
        if isinstance(span, TextSpan) and not span.content:
            continue
        if isinstance(span, HyperlinkSpan):
            children = _drop_empty_text_spans(list(span.content))
            non_link_children = [child for child in children if not isinstance(child, HyperlinkSpan)]
            if not non_link_children:
                continue
            span.content = non_link_children
        result.append(span)
    return result


def _slice_inline_span(span: InlineSpan, start: int, end: int) -> InlineSpan | None:
    """裁剪单个 Span 的局部可见区间。"""
    if start >= end:
        return None
    if isinstance(span, TextSpan):
        content = span.content[start:end]
        return span.model_copy(update={"content": content}) if content else None
    if isinstance(span, EquationInlineSpan):
        content = span.content[start:end]
        return span.model_copy(update={"content": content}) if content.strip() else None
    if isinstance(span, CodeInlineSpan):
        content = span.content[start:end]
        return span.model_copy(update={"content": content}) if content else None
    if isinstance(span, HyperlinkSpan):
        children = slice_inline_spans(span.content, start, end)
        non_link_children = [child for child in children if not isinstance(child, HyperlinkSpan)]
        return span.model_copy(update={"content": non_link_children}) if non_link_children else None
    return None


__all__ = [
    "inline_plain_text",
    "join_inline_spans",
    "map_text_span_content",
    "normalize_inline_spans",
    "replace_inline_text",
    "slice_inline_spans",
    "strip_inline_spans",
]
