"""Complement the word boundary when the natural language chunk is finally materialized to avoid affecting the classification of codes, formulas and layouts."""

from dataclasses import dataclass

from .....document.pdf.text.spacing import needs_tight_space
from .....foundation._text import is_hyphen_at_line_end
from .common import _normalize_match_fragment
from .matching import _assign_lines_to_blocks, _project_content_chars, _match_line_without_terminal_hyphen


@dataclass(frozen=True, slots=True)
class PDFTextSpacingLine:
    """Only compact line evidence of missing word boundaries is retained, without extending the lifetime of character geometry."""

    bbox: tuple[float, float, float, float]
    text: str
    source_index: int
    boundaries: tuple[int, ...]


def prepare_spacing_lines(lines, space_before=None):
    """Recoverable word boundary offsets are extracted by the original visual line, and the original line text and characters remain read-only."""
    if space_before is not None and not space_before:
        return []
    output = []
    for line in lines:
        if line.formula_candidate_only:
            continue
        positions = {
            index
            for index in range(1, len(line.chars))
            if (
                needs_tight_space(line.chars[index - 1], line.chars[index])
                if space_before is None
                else line.chars[index].get("char_idx") in space_before
                and line.chars[index - 1].get("char_idx", -2) + 1 == line.chars[index].get("char_idx")
            )
        }
        # Even if there is no new word boundary, the first and last context of word break and line continuation will be retained, and the existing hyphen projection rules will be reused.
        continuation = bool(
            output and output[-1].source_index + 1 == line.source_index and is_hyphen_at_line_end(output[-1].text)
        )
        if not positions and not continuation:
            continue
        parts, boundaries = [], []
        cursor = 0
        for index, char in enumerate(line.chars):
            if index in positions:
                boundaries.append(cursor)
            fragment = _normalize_match_fragment(char.get("char"))
            parts.append(fragment)
            cursor += len(fragment)
        if boundaries or continuation:
            output.append(PDFTextSpacingLine(line.bbox, "".join(parts), line.source_index, tuple(boundaries)))
    return output


def apply_spacing_lines(blocks, lines, page_size):
    """Only whitespace is added to natural language lines that match a complete match and have no separators in the original text."""
    if not lines:
        return
    for block_index, assigned in _assign_lines_to_blocks(blocks, lines, page_size).items():
        block = blocks[block_index]
        content = block["content"]
        projected = _project_content_chars(content)
        text = "".join(token.value for token in projected)
        offsets = set()
        cursor = 0
        for line_index, line in enumerate(assigned):
            start = text.find(line.text, cursor)
            if start < 0:
                next_line = assigned[line_index + 1] if line_index + 1 < len(assigned) else None
                match = _match_line_without_terminal_hyphen(text, line, next_line, cursor)
                if match is None:
                    continue
                start, cursor = match.start, match.end
            else:
                cursor = start + len(line.text)
            for boundary in line.boundaries:
                index = start + boundary
                if not 0 < index < len(projected):
                    continue
                left, right = projected[index - 1], projected[index]
                if left.raw_end == right.raw_start and not right.formula_gap_before:
                    offsets.add(right.raw_start)
        if offsets:
            parts, cursor = [], 0
            for offset in sorted(offsets):
                parts.extend((content[cursor:offset], " "))
                cursor = offset
            parts.append(content[cursor:])
            block["content"] = "".join(parts)
