"""HTML renderer Use strict GFM pipe table conversion."""

from __future__ import annotations

import re

from ....schema import TextSpan
from .inline import HtmlInlineResult, render_inline_content_html

_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")


def looks_like_gfm_table(content: str) -> bool:
    """Determine whether the text has the header and separated line shapes of GFM pipe table."""
    lines = [line.strip() for line in content.strip().splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    if not _contains_unescaped_pipe(lines[0]) or not _contains_unescaped_pipe(lines[1]):
        return False
    separator_cells = _split_pipe_row(lines[1])
    return bool(separator_cells and all(_SEPARATOR_CELL_RE.fullmatch(cell.strip()) for cell in separator_cells))


def render_gfm_table_html(content: str) -> HtmlInlineResult | None:
    """Convert strict, equal-width simple GFM pipe table to semantic HTML."""
    lines = [line.strip() for line in content.strip().splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    if not _contains_unescaped_pipe(lines[0]) or not _contains_unescaped_pipe(lines[1]):
        return None
    header = _split_pipe_row(lines[0])
    separators = _split_pipe_row(lines[1])
    if not header or len(header) != len(separators):
        return None
    if not all(_SEPARATOR_CELL_RE.fullmatch(cell.strip()) for cell in separators):
        return None

    rows = [_split_pipe_row(line) for line in lines[2:]]
    if any(len(row) != len(header) for row in rows):
        return None

    alignments = [_separator_alignment(cell.strip()) for cell in separators]
    has_math = False

    def _render_row(cells: list[str], cell_tag: str) -> str:
        """Renders a row of equal-width cells with accumulated formula presence markers."""
        nonlocal has_math
        rendered_cells: list[str] = []
        for index, cell in enumerate(cells):
            cell_text = _unescape_gfm_cell(cell.strip())
            rendered = (
                render_inline_content_html([TextSpan(type="text", content=cell_text)]) if cell_text else HtmlInlineResult("")
            )
            has_math = rendered.has_math or has_math
            alignment = alignments[index]
            class_attr = f' class="docvortex-align-{alignment}"' if alignment else ""
            rendered_cells.append(f"<{cell_tag}{class_attr}>{rendered.html}</{cell_tag}>")
        return f"<tr>{''.join(rendered_cells)}</tr>"

    table = "".join(
        [
            '<table class="docvortex-chart-table">',
            f"<thead>{_render_row(header, 'th')}</thead>",
            f"<tbody>{''.join(_render_row(row, 'td') for row in rows)}</tbody>",
            "</table>",
        ]
    )
    return HtmlInlineResult(table, has_math)


def _split_pipe_row(line: str) -> list[str]:
    """Split GFM table rows by vertical bars not escaped by an odd number of backslashes."""
    normalized = line.strip()
    if normalized.startswith("|"):
        normalized = normalized[1:]
    if normalized.endswith("|") and not _is_escaped(normalized, len(normalized) - 1):
        normalized = normalized[:-1]

    cells: list[str] = []
    start = 0
    for index, char in enumerate(normalized):
        if char == "|" and not _is_escaped(normalized, index):
            cells.append(normalized[start:index])
            start = index + 1
    cells.append(normalized[start:])
    return cells


def _is_escaped(content: str, index: int) -> bool:
    """Determine whether pipe is immediately adjacent to any backslash according to the markdown-it table rule."""
    return index > 0 and content[index - 1] == "\\"


def _contains_unescaped_pipe(content: str) -> bool:
    """Determines whether the row contains at least one true GFM column delimiter."""
    return any(char == "|" and not _is_escaped(content, index) for index, char in enumerate(content))


def _unescape_gfm_cell(content: str) -> str:
    """Reverse the original backslashes and vertical bars in GFM cells using the existing 2n+1 encoding."""

    def _replace(match: re.Match[str]) -> str:
        """Restore the 2n+1 Markdown backslashes before the vertical bar to n."""
        slash_count = len(match.group("slashes"))
        return "\\" * ((slash_count - 1) // 2) + "|"

    return re.sub(r"(?P<slashes>\\+)\|", _replace, content)


def _separator_alignment(cell: str) -> str | None:
    """Resolve left, center or right alignments from GFM delimited cells."""
    if cell.startswith(":") and cell.endswith(":"):
        return "center"
    if cell.endswith(":"):
        return "right"
    if cell.startswith(":"):
        return "left"
    return None


__all__ = ["looks_like_gfm_table", "render_gfm_table_html"]
