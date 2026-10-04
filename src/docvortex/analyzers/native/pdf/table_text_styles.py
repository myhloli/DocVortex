"""Recover subscripts and superscripts for high-confidence tables HTML using PDF native character geometry."""

from __future__ import annotations

import html
from collections import defaultdict
from typing import Any

from ....document.pdf.text._contracts import Char
from ....document.pdf.text.spacing import needs_tight_space
from ....schema import BBox
from ._script_geometry import ScriptRole
from ._table_recovery.candidate import serialize_native_table_html
from ._table_recovery.contracts import (
    NativeTableCell,
    NativeTableGlyph,
    NativeTableInput,
    NativeTableResult,
    NativeTableRule,
    NativeTableText,
)
from ._table_recovery.geometry import page_bbox_to_table_local
from ._table_recovery.text import build_cell_text_parts
from .geometry import _bbox_union_many, _coerce_bbox
from .inline.scripts import (
    _classify_script_runs,
    _fraction_member_indices,
    _prepare_fraction_rules,
    _script_line_char_roles,
)
from .inline.detection import _char_font_styles, _pdf_font_metadata
from .inline.types import PDF_FONT_ITALIC_FLAG
from .line_merging import _merge_overlapping_inline_text_clusters
from .models import _LineItem
from .native_text import _fill_native_typography

_OWNED_CELL_BATCH_CHAR_LIMIT = 8192
_owned_cell_batches = 0
_owned_cell_lines = 0
_owned_cell_fallbacks = 0


def table_script_stats() -> tuple[int, int, int]:
    """The report table has subscripts owned for the batch, the number of hit rows, and the number of rollbacks for the entire batch."""
    return _owned_cell_batches, _owned_cell_lines, _owned_cell_fallbacks


def _table_char_map(chars: tuple[Char, ...]) -> dict[int, Char]:
    """Create a page character query table according to the legal char_idx, and retain the first item in the repeated index."""

    output: dict[int, Char] = {}
    for fallback_index, char in enumerate(chars):
        char_idx = char.get("char_idx", fallback_index)
        if isinstance(char_idx, bool) or not isinstance(char_idx, int):
            continue
        output.setdefault(char_idx, char)
    return output


def _cell_glyphs(
    result: NativeTableResult,
    cell: NativeTableCell,
    glyph_by_source: dict[int, NativeTableGlyph] | None = None,
) -> list[NativeTableGlyph]:
    """Collect and restore visual inline order by stable character source for cell."""

    if glyph_by_source is None:
        glyph_by_source = {glyph.source_index: glyph for glyph in result.text.glyphs}
    return sorted(
        (glyph_by_source[source_index] for source_index in cell.source_char_indices if source_index in glyph_by_source),
        key=lambda glyph: (glyph.visual_row, glyph.bbox[0], glyph.bbox[1], glyph.glyph_id),
    )


def _cell_visual_lines(
    glyphs: list[NativeTableGlyph],
    chars_by_source: dict[int, Char],
    page_size: tuple[float, float],
    angle: int,
) -> list[_LineItem]:
    """Convert a cell glyph to visual row into lines used by text formula segments."""

    grouped: dict[int, list[NativeTableGlyph]] = defaultdict(list)
    for glyph in glyphs:
        grouped[glyph.visual_row].append(glyph)

    lines: list[_LineItem] = []
    for visual_row, row_glyphs in sorted(grouped.items()):
        # Input glyph is sorted by visual_row, x, y, and glyph_id; group traversal remains
        # This order, so the stable sorting is no longer repeated for the same visual row.
        ordered = row_glyphs
        chars = [chars_by_source[glyph.source_index] for glyph in ordered if glyph.source_index in chars_by_source]
        bboxes = [bbox for char in chars if (bbox := _coerce_bbox(char.get("bbox"))) is not None]
        if not chars or not bboxes:
            continue
        line = _LineItem(
            text="".join(glyph.text for glyph in ordered),
            bbox=_bbox_union_many(bboxes),
            angle=angle,
            source_index=visual_row,
            chars=chars,
            visual_row_id=visual_row,
        )
        lines.append(line)
    # The typesetting feature is only used for two-dimensional merging of multiple lines; a single line will enter the character script judgment as it is.
    # This path only reads the chars/angle/formula area, not line-level fonts or height statistics.
    if len(lines) > 1:
        for line in lines:
            _fill_native_typography(line, page_size)
    return lines


def _rule_overlaps_boundary(
    rule_bbox: BBox,
    boundary_y: float,
    boundary_left: float,
    boundary_right: float,
    tolerance: float,
) -> bool:
    """Determine whether the local horizontal line coincides with the horizontal boundary of a logical cell."""

    rule_y = (rule_bbox[1] + rule_bbox[3]) / 2.0
    if abs(rule_y - boundary_y) > tolerance:
        return False
    overlap = min(rule_bbox[2], boundary_right) - max(rule_bbox[0], boundary_left)
    rule_width = rule_bbox[2] - rule_bbox[0]
    boundary_width = boundary_right - boundary_left
    return overlap > 0 and overlap / max(1e-6, min(rule_width, boundary_width)) >= 0.5


def _non_grid_fraction_rules(
    table_input: NativeTableInput,
    result: NativeTableResult,
) -> list[NativeTableRule]:
    """Remove horizontal lines that coincide with the restored cell boundary, leaving only possible fraction lines."""

    tolerance = max(0.75, 0.12 * result.text.median_glyph_height)
    output: list[NativeTableRule] = []
    for rule in table_input.drawing_lines:
        if rule.orientation != "horizontal":
            continue
        local_bbox = page_bbox_to_table_local(rule.bbox, table_input.table_bbox, table_input.angle)
        if local_bbox is None:
            continue
        is_grid_rule = any(
            _rule_overlaps_boundary(
                local_bbox,
                boundary_y,
                cell.bbox[0],
                cell.bbox[2],
                max(tolerance, rule.width),
            )
            for cell in result.cells
            for boundary_y in (cell.bbox[1], cell.bbox[3])
        )
        if not is_grid_rule:
            output.append(rule)
    return output


def _drop_shifted_word_prefixes(chars: list[dict[str, Any]], roles: list[ScriptRole]) -> None:
    """Weak candidates with only the first letter offset of common alphabetic words are rejected, and complete homo-baseline words are closed by the refinement layer."""

    start = 0
    while start < len(chars):
        if not str(chars[start].get("char", "")).isalpha():
            start += 1
            continue
        end = start + 1
        while end < len(chars) and str(chars[end].get("char", "")).isalpha():
            end += 1
        if end - start >= 2 and roles[start] != "body" and all(role == "body" for role in roles[start + 1 : end]):
            roles[start] = "body"
        start = end


def _roles_by_source(chars: list[dict[str, Any]], roles: list[ScriptRole]) -> dict[int, ScriptRole]:
    """Aggregate roles by source number; origin conflicts are downgraded to body using the original implementation."""
    output: dict[int, ScriptRole] = {}
    for char, role in zip(chars, roles, strict=True):
        char_idx = char.get("char_idx")
        if not isinstance(char_idx, int) or role == "body":
            continue
        previous = output.get(char_idx)
        output[char_idx] = role if previous in {None, role} else "body"
    return {source_index: role for source_index, role in output.items() if role != "body"}


def _cell_script_roles(
    glyphs: list[NativeTableGlyph],
    chars_by_source: dict[int, Char],
    page_size: tuple[float, float],
    angle: int,
    tight_bboxes: dict[int, BBox],
    origins: dict[int, tuple[float, float]],
    fraction_members: set[int],
    *,
    visual_lines: list[_LineItem] | None = None,
) -> dict[int, ScriptRole]:
    """In cell, the stable character is returned according to the segmentation and character geometry of the same two-dimensional formula as the text."""

    lines = _cell_visual_lines(glyphs, chars_by_source, page_size, angle) if visual_lines is None else visual_lines
    if not lines:
        return {}
    segmented_lines = _merge_overlapping_inline_text_clusters(lines, page_size, [])
    roles_by_source: dict[int, ScriptRole] = {}
    for line in segmented_lines:
        chars, roles, _body_counts, _formula_flags = _script_line_char_roles(
            line,
            page_size,
            tight_bboxes,
            origins,
            fraction_members,
        )
        _drop_shifted_word_prefixes(chars, roles)
        for char, role in zip(chars, roles, strict=True):
            char_idx = char.get("char_idx")
            if not isinstance(char_idx, int) or role == "body":
                continue
            previous = roles_by_source.get(char_idx)
            roles_by_source[char_idx] = role if previous in {None, role} else "body"
    return {source_index: role for source_index, role in roles_by_source.items() if role != "body"}


def _owned_cell_line_indices(line: _LineItem, identities: dict[int, int]) -> list[int] | None:
    """Only rows with normal 0 degrees, same origin, and no special formula tags can go into the page-level owned batch."""
    if (
        type(line) is not _LineItem
        or type(line.chars) is not list
        or line.angle != 0
        or line.inline_math_regions
        or line.compact_formula_cluster
        or line.restored_inline_cluster
    ):
        return None
    indices: list[int] = []
    for char in line.chars:
        index = identities.get(id(char)) if type(char) is dict else None
        if index is None:
            return None
        indices.append(index)
    return indices


def _owned_cell_classify(
    pending: list[tuple[tuple[int, int], list[_LineItem], list[dict[str, Any]], list[int]]],
    owned_inputs: tuple[Any, dict[int, int]],
    page_size: tuple[float, float],
    tight_bboxes: dict[int, BBox],
    origins: dict[int, tuple[float, float]],
) -> dict[tuple[int, int], dict[int, ScriptRole]]:
    """Merge visual lines from multiple homologous cell into bounded owned batches and preserve the original refinement rules."""
    global _owned_cell_batches, _owned_cell_lines, _owned_cell_fallbacks
    evidence, identities = owned_inputs
    output: dict[tuple[int, int], dict[int, ScriptRole]] = {}
    cell_lines = {key: lines for key, lines, _chars, _indices in pending}
    records = []
    for key, lines, _chars, cell_indices in pending:
        offset = 0
        for line in lines:
            width = len(line.chars)
            records.append((key, line, cell_indices[offset : offset + width]))
            offset += width
    start = 0
    while start < len(records):
        end = start + 1
        char_count = len(records[start][2])
        while end < len(records) and (char_count + len(records[end][2]) <= _OWNED_CELL_BATCH_CHAR_LIMIT or char_count == 0):
            char_count += len(records[end][2])
            end += 1
        batch = records[start:end]
        flat_indices = [index for _key, _line, indices in batch for index in indices]
        offsets = [0]
        for _key, _line, indices in batch:
            offsets.append(offsets[-1] + len(indices))
        classified = evidence.classify_indices(flat_indices, offsets)
        if len(classified) != len(batch) or any(
            item is None or len(item) != len(indices) for item, (_key, _line, indices) in zip(classified, batch, strict=True)
        ):
            fallback_keys = {key for key, _line, _indices in batch}
            _owned_cell_fallbacks += sum(key in cell_lines for key in fallback_keys)
            for key in fallback_keys:
                output[key] = _cell_script_roles_from_lines(cell_lines[key], page_size, tight_bboxes, origins)
            start = end
            continue
        _owned_cell_batches += 1
        _owned_cell_lines += len(batch)
        # Table cell has different semantics than the large two-dimensional merge of text rows; when any non-text role is given first,
        # The entire cell returns to the existing reference judgment, and only the result of determining the full text is directly reused in the owned batch.
        fallback_keys = {
            key
            for (key, _line, _indices), raw_roles in zip(batch, classified, strict=True)
            if any(role != 0 for role in raw_roles)
        }
        for key in fallback_keys:
            output.pop(key, None)
            _owned_cell_fallbacks += 1
            output[key] = _cell_script_roles_from_lines(cell_lines[key], page_size, tight_bboxes, origins)
        for (key, line, _indices), raw_roles in zip(batch, classified, strict=True):
            if key in fallback_keys:
                continue
            roles_by_source = output.setdefault(key, {})
            chars = line.chars
            memberships = [None] * len(chars)
            roles, _body_counts, _formula_flags = _classify_script_runs(
                chars,
                tight_bboxes,
                origins,
                memberships,
                preclassified_native=[raw_roles],
            )
            _drop_shifted_word_prefixes(chars, roles)
            for char, role in zip(chars, roles, strict=True):
                char_idx = char.get("char_idx")
                if not isinstance(char_idx, int) or role == "body":
                    continue
                previous = roles_by_source.get(char_idx)
                roles_by_source[char_idx] = role if previous in {None, role} else "body"
        start = end
    for key, roles in list(output.items()):
        output[key] = {source_index: role for source_index, role in roles.items() if role != "body"}
    return output


def _cell_script_roles_from_lines(
    lines: list[_LineItem],
    page_size: tuple[float, float],
    tight_bboxes: dict[int, BBox],
    origins: dict[int, tuple[float, float]],
) -> dict[int, ScriptRole]:
    """Execute reference paths with cached cell visual lines to avoid duplicate group lines if owned is unavailable."""
    return _cell_script_roles(
        [],
        {},
        page_size,
        0,
        tight_bboxes,
        origins,
        set(),
        visual_lines=lines,
    )


def _render_styled_cell(
    cell: NativeTableCell,
    glyphs: list[NativeTableGlyph],
    median_height: float,
    roles: dict[int, ScriptRole],
    *,
    chars_by_source: dict[int, Char] | None = None,
    tight_bboxes: dict[int, BBox] | None = None,
    origins: dict[int, tuple[float, float]] | None = None,
    preserve_font_styles: bool = False,
) -> str:
    """Safely insert superscripts, subscripts and optional bold italics according to the original character source, and maintain escape fallback when the text does not match."""

    parts = build_cell_text_parts(glyphs, median_height)
    if "".join(text for text, _source_index in parts) != cell.content:
        return html.escape(cell.content, quote=False)

    rendered: list[str] = []
    active_role: ScriptRole = "body"
    active_styles = (False, False)
    active_parts: list[str] = []

    def flush() -> None:
        """Submit fragments with the same character and font style, only generate fixed tags and escape the original text."""

        nonlocal active_parts
        if not active_parts:
            return
        content = html.escape("".join(active_parts), quote=False)
        if active_styles[1]:
            content = f"<i>{content}</i>"
        if active_styles[0]:
            content = f"<b>{content}</b>"
        if active_role == "sup":
            rendered.append(f"<sup>{content}</sup>")
        elif active_role == "sub":
            rendered.append(f"<sub>{content}</sub>")
        else:
            rendered.append(content)
        active_parts = []

    previous_source = None
    for text, source_index in parts:
        role = roles.get(source_index, "body") if source_index is not None else "body"
        styles = active_styles if source_index is None else (False, False)
        if preserve_font_styles and chars_by_source is not None and source_index in chars_by_source:
            char = chars_by_source[source_index]
            styles = ("bold" in _char_font_styles(char), bool(_pdf_font_metadata(char)[1] & PDF_FONT_ITALIC_FLAG))
        if (
            chars_by_source is not None
            and role == active_role == "body"
            and previous_source in chars_by_source
            and source_index in chars_by_source
            and needs_tight_space(
                chars_by_source[previous_source],
                chars_by_source[source_index],
                tight_bboxes=tight_bboxes,
                origins=origins,
            )
        ):
            active_parts.append(" ")
        if role != active_role or styles != active_styles:
            flush()
            active_role = role
            active_styles = styles
        active_parts.append(text)
        previous_source = source_index
    flush()
    return "".join(rendered)


def render_native_table_html_with_scripts(
    result: NativeTableResult,
    table_input: NativeTableInput,
    tight_bboxes: dict[int, BBox],
    origins: dict[int, tuple[float, float]],
    *,
    _owned_script_inputs: tuple[Any, dict[int, int]] | None = None,
    preserve_font_styles: bool = False,
) -> str:
    """Restore subscripts and superscripts for high-confidence native tables, returning original HTML when evidence is insufficient."""

    if not tight_bboxes or not origins:
        return result.html
    if not isinstance(getattr(result, "text", None), NativeTableText):
        return result.html
    chars_by_source = _table_char_map(table_input.chars)
    fraction_rules = _non_grid_fraction_rules(table_input, result)
    prepared_fraction_rules = _prepare_fraction_rules(fraction_rules, table_input.page_size, table_input.angle)
    # A source index is only created once for a table; the consequent coverage semantics of the original dictionary derivation are maintained.
    glyph_by_source = {glyph.source_index: glyph for glyph in result.text.glyphs}
    cell_glyphs = {(cell.row, cell.col): _cell_glyphs(result, cell, glyph_by_source) for cell in result.cells}
    cell_lines = {
        (cell.row, cell.col): _cell_visual_lines(
            cell_glyphs[(cell.row, cell.col)],
            chars_by_source,
            table_input.page_size,
            table_input.angle,
        )
        for cell in result.cells
    }
    owned_pending: list[tuple[tuple[int, int], list[_LineItem], list[dict[str, Any]], list[int]]] = []
    fallback_cells: list[tuple[tuple[int, int], set[int]]] = []
    for cell in result.cells:
        key = (cell.row, cell.col)
        glyphs = cell_glyphs[key]
        cell_chars = [chars_by_source[glyph.source_index] for glyph in glyphs if glyph.source_index in chars_by_source]
        fraction_members = _fraction_member_indices(
            table_input.page_size,
            cell_chars,
            tight_bboxes,
            fraction_rules,
            table_input.angle,
            prepared_fraction_rules,
        )
        lines = cell_lines[key]
        indices = None
        if _owned_script_inputs is not None and table_input.angle == 0 and not fraction_members and lines:
            # owned The batch must consume exactly the same 2D merged rows as the reference path.
            segmented = _merge_overlapping_inline_text_clusters(lines, table_input.page_size, [])
            packed = [
                (line, line_indices)
                for line in segmented
                if (line_indices := _owned_cell_line_indices(line, _owned_script_inputs[1])) is not None
            ]
            if len(packed) == len(segmented):
                indices = [index for _line, row_indices in packed for index in row_indices]
                chars = [char for line in segmented for char in line.chars]
                owned_pending.append((key, segmented, chars, indices))
        if indices is None:
            fallback_cells.append((key, fraction_members))

    owned_roles = (
        _owned_cell_classify(
            owned_pending,
            _owned_script_inputs,
            table_input.page_size,
            tight_bboxes,
            origins,
        )
        if owned_pending
        else {}
    )
    cell_roles: dict[tuple[int, int], dict[int, ScriptRole]] = dict(owned_roles)
    for key, fraction_members in fallback_cells:
        roles = _cell_script_roles(
            cell_glyphs[key],
            chars_by_source,
            table_input.page_size,
            table_input.angle,
            tight_bboxes,
            origins,
            fraction_members,
            visual_lines=cell_lines[key],
        )
        if roles:
            cell_roles[key] = roles

    has_missing_space = False
    for cell in result.cells:
        key = (cell.row, cell.col)
        glyphs = cell_glyphs[key]
        roles = cell_roles.get(key, {})
        if not has_missing_space:
            has_missing_space = any(
                left.visual_row == right.visual_row
                and roles.get(left.source_index, "body") == roles.get(right.source_index, "body") == "body"
                and left.source_index in chars_by_source
                and right.source_index in chars_by_source
                and needs_tight_space(
                    chars_by_source[left.source_index],
                    chars_by_source[right.source_index],
                    tight_bboxes=tight_bboxes,
                    origins=origins,
                )
                for left, right in zip(glyphs, glyphs[1:])
            )
    if not cell_roles and not has_missing_space and not preserve_font_styles:
        return result.html
    return serialize_native_table_html(
        result.rows,
        result.cells,
        render_cell=lambda cell: _render_styled_cell(
            cell,
            cell_glyphs[(cell.row, cell.col)],
            result.text.median_glyph_height,
            cell_roles.get((cell.row, cell.col), {}),
            chars_by_source=chars_by_source,
            tight_bboxes=tight_bboxes,
            origins=origins,
            preserve_font_styles=preserve_font_styles,
        ),
    )


__all__ = ["render_native_table_html_with_scripts"]
