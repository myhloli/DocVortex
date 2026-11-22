from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from ....content.inline import inline_plain_text, join_inline_spans
from ....schema import (
    EquationInlineSpan,
    HyperlinkSpan,
    InlineSpan,
    InlineStyle,
    ListBlock,
    MiddleJson,
    PageBlock,
    PageFootnoteBlock,
    PageInfo,
    RefTextBlock,
    TextBlock,
    TextSpan,
    TitleBlockBase,
)
from ..common.anchors import iter_document_anchor_blocks

_SUPERSCRIPT = re.compile(r"(?:\{\})?\^\s*(?:\{\s*([0-9]+|[\*†‡]|\\(?:ast|dagger|ddagger))\s*\}|([0-9]+|[\*†‡]))")
_NOTE_PREFIX = re.compile(r"^\s*([0-9]+|\*|†|‡)(?:[.)])?\s+")
_NOTE_SYMBOLS = {
    "*": "asterisk",
    "†": "dagger",
    "‡": "double-dagger",
    r"\ast": "asterisk",
    r"\dagger": "dagger",
    r"\ddagger": "double-dagger",
}
_REFERENCE_PREFIX = re.compile(r"^\s*\[([0-9]+)\]\s+")
_CITATION = re.compile(r"(?<![\[\\])\[[0-9]+(?:\s*,\s*[0-9]+)*\](?![\[(])")
_NUMBER = re.compile(r"[0-9]+")


@dataclass
class _Footnote:
    label: str
    block: PageFootnoteBlock
    body: list[InlineSpan]
    target: str = ""


@dataclass
class PdfMarkdownLinks:
    document: MiddleJson
    footnote_targets: set[str]
    reference_targets: dict[str, str]


def prepare_pdf_markdown_links(document: MiddleJson) -> PdfMarkdownLinks:
    if document.metadata.file_suffix != "pdf":
        return PdfMarkdownLinks(document, set(), {})

    anchors = {(block.anchor or "").strip() for block in iter_document_anchor_blocks(document)}
    reference_counts = Counter(label for page in document.pages for block in page.blocks for label in _reference_labels(block))
    references = {label: _claim_target(f"ref-{label}", anchors) for label, count in reference_counts.items() if count == 1}
    notes, continuations = _collect_footnotes(document.pages)
    cited: set[str] = set()
    targets: set[str] = set()
    pages: list[PageInfo] = []
    for page in document.pages:
        page_notes = notes.get(page.page_idx, {})
        blocks = [_link_block(block, page_notes, references, cited, page.page_idx, anchors) for block in page.blocks]
        linked_notes = {id(note.block): note for note in page_notes.values() if note.target}
        rendered_blocks: list[PageBlock] = []
        for original, block in zip(page.blocks, blocks, strict=True):
            note = linked_notes.get(id(original))
            if note is not None:
                targets.add(note.target)
                block = note.block.model_copy(update={"anchor": note.target, "content": note.body})
            if (previous := continuations.get(id(original))) is not None and previous.target:
                continue
            rendered_blocks.append(block)
        pages.append(page.model_copy(update={"blocks": rendered_blocks}))
    if not targets and not cited:
        return PdfMarkdownLinks(document, set(), {})
    return PdfMarkdownLinks(
        document.model_copy(update={"pages": pages}), targets, {label: references[label] for label in cited}
    )


def reference_label(content: list[InlineSpan]) -> str | None:
    match = _REFERENCE_PREFIX.match(inline_plain_text(content))
    return match[1] if match else None


def _claim_target(base: str, anchors: set[str]) -> str:
    target = base
    suffix = 2
    while target in anchors:
        target = f"{base}-{suffix}"
        suffix += 1
    anchors.add(target)
    return target


def _reference_labels(block: PageBlock) -> list[str]:
    if isinstance(block, RefTextBlock) and not block.continues_prev and (label := reference_label(block.content)):
        return [label]
    if isinstance(block, ListBlock):
        return [label for child in block.content for label in _reference_labels(child)]
    return []


def _collect_footnotes(pages: list[PageInfo]) -> tuple[dict[int, dict[str, _Footnote]], dict[int, _Footnote]]:
    notes: dict[int, dict[str, _Footnote]] = {}
    continuations: dict[int, _Footnote] = {}
    previous: _Footnote | None = None
    previous_page = -2
    for page in pages:
        page_notes: list[_Footnote] = []
        footnotes = [block for block in page.blocks if isinstance(block, PageFootnoteBlock)]
        for index, block in enumerate(footnotes):
            note = _parse_footnote(block)
            if note is not None:
                page_notes.append(note)
            elif index == 0 and previous is not None and page.page_idx == previous_page + 1 and _continues(previous, block):
                previous.body = join_inline_spans([previous.body, block.content])
                continuations[id(block)] = previous
            else:
                previous = None
        counts = Counter(note.label for note in page_notes)
        notes[page.page_idx] = {note.label: note for note in page_notes if counts[note.label] == 1}
        if page_notes:
            last = page_notes[-1]
            previous = last if last.block is footnotes[-1] and counts[last.label] == 1 else None
        elif not footnotes:
            previous = None
        previous_page = page.page_idx
    return notes, continuations


def _continues(note: _Footnote, block: PageFootnoteBlock) -> bool:
    before = inline_plain_text(note.body).rstrip()
    after = inline_plain_text(block.content).lstrip()
    return bool(before and after and not block.anchor and before[-1] not in ".!?" and after[0].islower())


def _parse_footnote(block: PageFootnoteBlock) -> _Footnote | None:
    if block.anchor or not block.content:
        return None
    first, *body = block.content
    label = _superscript_label(first)
    if label is None and isinstance(first, TextSpan) and (match := _NOTE_PREFIX.match(first.content)):
        label = _NOTE_SYMBOLS.get(match[1], match[1])
        if remainder := first.content[match.end() :]:
            body.insert(0, first.model_copy(update={"content": remainder}))
    if label is None or not inline_plain_text(body).strip():
        return None
    return _Footnote(label, block, body)


def _superscript_label(span: InlineSpan) -> str | None:
    if isinstance(span, EquationInlineSpan) and (match := _SUPERSCRIPT.fullmatch(span.content.strip())):
        marker = match[1] or match[2]
    elif isinstance(span, TextSpan) and "superscript" in span.styles:
        marker = span.content.strip()
    else:
        return None
    return marker if _NUMBER.fullmatch(marker) else _NOTE_SYMBOLS.get(marker)


def _link_block(
    block: PageBlock, notes: dict[str, _Footnote], references: dict[str, str], cited: set[str], page_idx: int, anchors: set[str]
) -> PageBlock:
    if isinstance(block, ListBlock):
        children = [_link_block(item, notes, references, cited, page_idx, anchors) for item in block.content]
        return block if children == block.content else block.model_copy(update={"content": children})
    if not isinstance(block, (TextBlock, TitleBlockBase)):
        return block

    content: list[InlineSpan] = []
    for index, span in enumerate(block.content):
        label = _superscript_label(span)
        if label and label in notes:
            content.append(_footnote_link(notes[label], page_idx, anchors))
        elif label and index == len(block.content) - 1 and index and _ends_with_punctuation(block.content[index - 1]):
            if combined := _split_footnote_labels(label, notes):
                content.extend(_footnote_link(notes[item], page_idx, anchors) for item in combined)
            else:
                content.append(span)
        elif references and (
            isinstance(span, TextSpan)
            or isinstance(span, EquationInlineSpan)
            and index > 0
            and _follows_prose(block.content[index - 1])
        ):
            content.extend(_link_citations(span, references, cited))
        else:
            content.append(span)
    return block if content == block.content else block.model_copy(update={"content": content})


def _ends_with_punctuation(span: InlineSpan) -> bool:
    return isinstance(span, TextSpan) and span.content.rstrip().endswith((":", ".", "!", "?"))


def _follows_prose(span: InlineSpan) -> bool:
    if not isinstance(span, TextSpan):
        return False
    before = span.content.rstrip()
    return bool(before and (before[-1].isalpha() or before[-1] in ")\"'”’"))


def _split_footnote_labels(label: str, notes: dict[str, _Footnote]) -> tuple[str, str] | None:
    if not _NUMBER.fullmatch(label):
        return None
    candidates = [
        (left, right)
        for index in range(1, len(label))
        if (left := label[:index]) in notes and (right := label[index:]) in notes and int(right) == int(left) + 1
    ]
    return candidates[0] if len(candidates) == 1 else None


def _footnote_link(note: _Footnote, page_idx: int, anchors: set[str]) -> HyperlinkSpan:
    if not note.target:
        note.target = _claim_target(f"note-{page_idx + 1}-{note.label}", anchors)
    return _inline_link(note.label, note.target)


def _link_citations(span: TextSpan | EquationInlineSpan, references: dict[str, str], cited: set[str]) -> list[InlineSpan]:
    if isinstance(span, EquationInlineSpan) and not _CITATION.fullmatch(span.content.strip()):
        return [span]
    text = span.content
    styles = span.styles if isinstance(span, TextSpan) else []
    linked: list[InlineSpan] = []
    cursor = 0
    for match in _CITATION.finditer(text):
        numbers = list(_NUMBER.finditer(text, match.start(), match.end()))
        if not all(number[0] in references for number in numbers):
            continue
        for number in numbers:
            linked.append(TextSpan(type="text", content=text[cursor : number.start()], styles=styles))
            linked.append(_inline_link(number[0], references[number[0]], styles))
            cited.add(number[0])
            cursor = number.end()
    if not linked:
        return [span]
    linked.append(TextSpan(type="text", content=text[cursor:], styles=styles))
    return linked


def _inline_link(label: str, target: str, styles: list[InlineStyle] | None = None) -> HyperlinkSpan:
    return HyperlinkSpan(
        type="hyperlink", url=f"#{target}", content=[TextSpan(type="text", content=label, styles=styles or [])]
    )
