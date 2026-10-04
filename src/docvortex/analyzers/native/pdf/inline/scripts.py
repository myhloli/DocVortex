"""Identify superscript and subscript evidence by formula area and character geometry."""

from __future__ import annotations

from bisect import bisect_right
import statistics
import math
import unicodedata
from functools import lru_cache
from typing import Any, Literal, Sequence

from .....schema import BBox
from .....document.pdf.text._contracts import Bbox as CharBox
from .._script_geometry import (
    ScriptRole,
    _native_text_flags,
    _numeric_superscript_indices,
    _script_font_key,
    _coerce_finite_bbox,
    build_script_features,
    classify_char_script_roles,
    paired_script_roles,
)
from ..geometry import _rotate_bbox_to_upright
from .common import _coerce_bbox, _normalize_match_fragment, _ordered_line_chars
from .types import (
    _PDF_SCRIPT_AUTHOR_MARKS,
    _PDF_SCRIPT_CITATION_BRACKETS,
    _PDF_SCRIPT_COMPACT_JOINERS,
    _PDF_SCRIPT_MATH_BASE_CHARS,
    _PDF_SCRIPT_SIGN_CHARS,
    _PDF_SCRIPT_SPACED_OPERATORS,
    _PDF_SCRIPT_TOKEN_CONNECTORS,
    _PDF_SCRIPT_TRAILING_MARKS,
    PDFTextScriptLine,
    PDFTextScriptRange,
)


def _rotate_origin_to_upright(
    origin: tuple[float, float],
    page_size: tuple[float, float],
    angle: int,
) -> tuple[float, float]:
    """Rotate page character origin to the local forward coordinate of the current Flash row."""
    x, y = origin
    page_width, page_height = page_size
    if angle == 270:
        return page_height - y, x
    if angle == 90:
        return y, page_width - x
    if angle == 180:
        return page_width - x, page_height - y
    return origin


def _bbox_center_inside_region(bbox: BBox, region: BBox) -> bool:
    """Determine whether the center of characters tight bbox falls into the formula area."""
    center_x = (bbox[0] + bbox[2]) / 2
    center_y = (bbox[1] + bbox[3]) / 2
    return region[0] <= center_x <= region[2] and region[1] <= center_y <= region[3]


def _script_region_memberships(
    chars: list[dict[str, Any]],
    tight_bboxes: dict[int, BBox],
    regions: list[BBox],
) -> list[int | None]:
    """Allocate characters to the formula area according to the center of the page tight, and return to None outside the area."""
    memberships: list[int | None] = []
    for char in chars:
        char_idx = char.get("char_idx")
        tight_bbox = tight_bboxes.get(char_idx) if isinstance(char_idx, int) else None
        region_index = None
        if tight_bbox is not None:
            region_index = next(
                (index for index, region in enumerate(regions) if _bbox_center_inside_region(tight_bbox, region)),
                None,
            )
        memberships.append(region_index)
    return memberships


def _script_char_text(char: dict[str, Any]) -> str:
    """Returns the stable text used by single-character script determination."""
    value = char.get("char", "")
    return value if type(value) is str else str(value)


def _is_cjk_text(text: str) -> bool:
    """Determines whether a single character belongs to CJK, Japanese Kana, or Korean writing systems."""
    if len(text) != 1:
        return False
    codepoint = ord(text)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


@lru_cache(maxsize=8192)
def _is_math_identifier_char(text: str) -> bool:
    """Identifies alphanumeric characters that can be combined with Latin base/index to form math token."""
    if len(text) != 1 or _is_cjk_text(text):
        return False
    if text.isascii():
        return text.isalnum()
    if "０" <= text <= "９":
        return True
    category = unicodedata.category(text)
    unicode_name = unicodedata.name(text, "")
    return (
        text in _PDF_SCRIPT_MATH_BASE_CHARS
        or "GREEK" in unicode_name
        or "MATHEMATICAL" in unicode_name
        or category in {"Lu", "Ll", "Lm"}
    )


def _is_math_script_token_char(text: str) -> bool:
    """Determines whether a character is a mathematical token that can be re-anchored by source order."""
    return _is_math_identifier_char(text) or text in _PDF_SCRIPT_TOKEN_CONNECTORS


def _iter_math_script_tokens(chars: list[dict[str, Any]]) -> list[list[int]]:
    """Split local token by continuous math identifier and connectors, and break at CJK boundaries."""
    tokens: list[list[int]] = []
    current: list[int] = []
    for index, char in enumerate(chars):
        if _is_math_script_token_char(_script_char_text(char)):
            current.append(index)
            continue
        if current:
            tokens.append(current)
            current = []
    if current:
        tokens.append(current)
    return tokens


def _citation_script_indices(chars: list[dict[str, Any]], roles: list[ScriptRole]) -> set[int]:
    """Recognize square bracket reference intervals to avoid conservative token rule deletion of numeric references."""
    protected: set[int] = set()
    for start, char in enumerate(chars):
        closing = _PDF_SCRIPT_CITATION_BRACKETS.get(_script_char_text(char))
        if closing is None:
            continue
        for end in range(start + 1, min(len(chars), start + 16)):
            if _script_char_text(chars[end]) != closing:
                continue
            if any(roles[index] != "body" and _script_char_text(chars[index]).isalnum() for index in range(start + 1, end)):
                protected.update(range(start, end + 1))
            break
    return protected


def _token_origin(
    char: dict[str, Any],
    origins: dict[int, tuple[float, float]],
) -> float | None:
    """Read token character partial forward origin y."""
    char_idx = char.get("char_idx")
    origin = origins.get(char_idx) if isinstance(char_idx, int) else None
    return float(origin[1]) if origin is not None else None


def _token_tight_height(
    char: dict[str, Any],
    tight_bboxes: dict[int, BBox],
) -> float:
    """Read the local forward tight height of the token character."""
    char_idx = char.get("char_idx")
    bbox = tight_bboxes.get(char_idx) if isinstance(char_idx, int) else None
    return max(0.0, bbox[3] - bbox[1]) if bbox is not None else 0.0


def _has_adjacent_math_base(
    chars: list[dict[str, Any]],
    index: int,
    roles: list[ScriptRole],
    tight_bboxes: dict[int, BBox],
    origins: dict[int, tuple[float, float]],
) -> bool:
    """Determines whether there is a non-CJK immediately to the left of the isolated index with a clear displacement Math base."""
    if index <= 0 or roles[index - 1] != "body":
        return False
    base_text = _script_char_text(chars[index - 1])
    if not _is_math_identifier_char(base_text):
        return False
    base_origin = _token_origin(chars[index - 1], origins)
    script_origin = _token_origin(chars[index], origins)
    base_height = _token_tight_height(chars[index - 1], tight_bboxes)
    base_idx = chars[index - 1].get("char_idx")
    script_idx = chars[index].get("char_idx")
    base_bbox = tight_bboxes.get(base_idx) if isinstance(base_idx, int) else None
    script_bbox = tight_bboxes.get(script_idx) if isinstance(script_idx, int) else None
    if base_origin is None or script_origin is None or base_bbox is None or script_bbox is None:
        return False
    return (
        _bbox_axis_overlap(base_bbox, script_bbox, axis="y") > 0
        or _horizontal_gap_between_bboxes(base_bbox, script_bbox) <= max(2.0, 0.5 * base_height)
    ) and abs(script_origin - base_origin) >= max(0.35, 0.08 * base_height)


def _horizontal_gap_between_bboxes(first: BBox, second: BBox) -> float:
    """Returns the horizontal gap between two tight bbox."""
    return max(0.0, first[0] - second[2], second[0] - first[2])


def _token_split_position(
    chars: list[dict[str, Any]],
    token: list[int],
    roles: list[ScriptRole],
    tight_bboxes: dict[int, BBox],
    origins: dict[int, tuple[float, float]],
) -> int | None:
    """Use the leftmost origin cluster and an explicit join character to delimit the base from the index."""
    alnum_positions = [index for index in token if _is_math_identifier_char(_script_char_text(chars[index]))]
    if len(alnum_positions) < 2:
        return None
    first = alnum_positions[0]
    first_origin = _token_origin(chars[first], origins)
    first_height = _token_tight_height(chars[first], tight_bboxes)
    origin_tolerance = max(0.35, 0.06 * first_height)
    leading_connectors = [
        index for index in token if index < first and _script_char_text(chars[index]) in _PDF_SCRIPT_TOKEN_CONNECTORS
    ]
    if leading_connectors:
        return alnum_positions[1]
    scripted_positions = [position for position in alnum_positions[1:] if roles[position] != "body"]
    if roles[first] == "body" and scripted_positions:
        first_scripted = scripted_positions[0]
        prefix = [index for index in token if index < first_scripted]
        suffix = [index for index in token if index >= first_scripted]
        if (
            len(prefix) >= 2
            and all(roles[index] == "body" and _script_char_text(chars[index]).isalpha() for index in prefix)
            and all(roles[index] == "sup" and _script_char_text(chars[index]).isdigit() for index in suffix)
        ):
            # The numerical superscripts after names and words already have clear boundaries and do not need to be re-segmented by descending glyphs within the text.
            return first_scripted
    for position in alnum_positions[1:]:
        if any(_script_char_text(chars[index]) in _PDF_SCRIPT_TOKEN_CONNECTORS for index in range(first + 1, position)):
            return position
        origin = _token_origin(chars[position], origins)
        if first_origin is not None and origin is not None and abs(origin - first_origin) > origin_tolerance:
            return position
    if roles[first] == "body" and scripted_positions:
        return scripted_positions[0]
    return None


def _script_geometry_is_aligned(
    chars: list[dict[str, Any]],
    first: int,
    second: int,
    tight_bboxes: dict[int, BBox],
    origins: dict[int, tuple[float, float]],
) -> bool:
    """Determine whether two characters are on the same displaced baseline."""
    first_origin = _token_origin(chars[first], origins)
    second_origin = _token_origin(chars[second], origins)
    first_height = _token_tight_height(chars[first], tight_bboxes)
    second_height = _token_tight_height(chars[second], tight_bboxes)
    first_idx = chars[first].get("char_idx")
    second_idx = chars[second].get("char_idx")
    first_bbox = tight_bboxes.get(first_idx) if isinstance(first_idx, int) else None
    second_bbox = tight_bboxes.get(second_idx) if isinstance(second_idx, int) else None
    if first_origin is None or second_origin is None or first_bbox is None or second_bbox is None:
        return False
    scale = max(first_height, second_height, 1.0)
    first_center = (first_bbox[1] + first_bbox[3]) / 2
    second_center = (second_bbox[1] + second_bbox[3]) / 2
    return abs(first_origin - second_origin) <= max(0.35, 0.06 * scale) and abs(first_center - second_center) <= max(
        0.75,
        0.3 * scale,
    )


def _nearest_nonspace_index(
    chars: list[dict[str, Any]],
    start: int,
    step: Literal[-1, 1],
) -> int | None:
    """Finds the nearest non-whitespace character forward or backward from the specified position."""
    index = start + step
    while 0 <= index < len(chars):
        if not _script_char_text(chars[index]).isspace():
            return index
        index += step
    return None


def _close_spaced_script_operators(
    chars: list[dict[str, Any]],
    roles: list[ScriptRole],
    tight_bboxes: dict[int, BBox],
    origins: dict[int, tuple[float, float]],
) -> None:
    """`1 - x` Class I subscript run across a small number of PDF spaces closing the same baseline."""
    for seed, role in enumerate(list(roles)):
        if role == "body" or not _is_math_identifier_char(_script_char_text(chars[seed])):
            continue
        operator_index = _nearest_nonspace_index(chars, seed, 1)
        if operator_index is None or operator_index - seed > 3:
            continue
        if _script_char_text(chars[operator_index]) not in _PDF_SCRIPT_SPACED_OPERATORS:
            continue
        target = _nearest_nonspace_index(chars, operator_index, 1)
        if target is None or target - operator_index > 3:
            continue
        if not _is_math_identifier_char(_script_char_text(chars[target])):
            continue
        if not _script_geometry_is_aligned(chars, seed, operator_index, tight_bboxes, origins):
            continue
        if not _script_geometry_is_aligned(chars, seed, target, tight_bboxes, origins):
            continue
        roles[operator_index] = role
        roles[target] = role


def _close_compact_aligned_script_suffixes(
    chars: list[dict[str, Any]],
    raw_roles: list[ScriptRole],
    refined_roles: list[ScriptRole],
    tight_bboxes: dict[int, BBox],
    origins: dict[int, tuple[float, float]],
) -> None:
    """Close the existing trusted corner mark run with the compact hyphen suffix of the baseline."""
    for joiner_index in range(1, len(chars) - 1):
        if _script_char_text(chars[joiner_index]) not in _PDF_SCRIPT_COMPACT_JOINERS:
            continue
        left_seed = joiner_index - 1
        role = refined_roles[left_seed]
        if role == "body" or not _is_math_identifier_char(_script_char_text(chars[left_seed])):
            continue

        left_start = left_seed
        while (
            left_start > 0
            and refined_roles[left_start - 1] == role
            and _is_math_identifier_char(_script_char_text(chars[left_start - 1]))
        ):
            left_start -= 1
        if left_seed - left_start + 1 < 2:
            continue
        anchor_index = left_start - 1
        if (
            anchor_index < 0
            or refined_roles[anchor_index] != "body"
            or not _is_math_identifier_char(_script_char_text(chars[anchor_index]))
        ):
            continue

        suffix_start = joiner_index + 1
        suffix_end = suffix_start
        while suffix_end < len(chars) and _is_math_identifier_char(_script_char_text(chars[suffix_end])):
            suffix_end += 1
        if suffix_end - suffix_start < 2:
            continue
        restored_indices = range(joiner_index, suffix_end)
        if any(raw_roles[index] != role for index in restored_indices):
            continue
        if not _script_geometry_is_aligned(chars, left_seed, joiner_index, tight_bboxes, origins):
            continue
        if any(
            not _script_geometry_is_aligned(chars, left_seed, index, tight_bboxes, origins)
            for index in range(suffix_start, suffix_end)
        ):
            continue
        refined_roles[joiner_index:suffix_end] = [role] * (suffix_end - joiner_index)


def _protected_subscript_indices(
    chars: list[dict[str, Any]],
    roles: list[ScriptRole],
    tokens: list[list[int]],
    tight_bboxes: dict[int, BBox],
    origins: dict[int, tuple[float, float]],
) -> set[int]:
    """Find subscript characters that have internal base or are connected to the same baseline."""
    protected: set[int] = set()
    for token in tokens:
        for position, index in enumerate(token):
            if roles[index] != "sub" or not _is_math_identifier_char(_script_char_text(chars[index])):
                continue
            if any(
                earlier < index and roles[earlier] == "body" and _is_math_identifier_char(_script_char_text(chars[earlier]))
                for earlier in token[:position]
            ) or _has_adjacent_math_base(chars, index, roles, tight_bboxes, origins):
                protected.add(index)
    changed = True
    while changed:
        changed = False
        for index, role in enumerate(roles):
            if role != "sub" or index in protected or not _is_math_identifier_char(_script_char_text(chars[index])):
                continue
            for seed in tuple(protected):
                start, end = sorted((seed, index))
                if end - start > 5 or not _script_geometry_is_aligned(chars, seed, index, tight_bboxes, origins):
                    continue
                if all(
                    _script_char_text(chars[bridge]).isspace()
                    or _script_char_text(chars[bridge]) in _PDF_SCRIPT_TOKEN_CONNECTORS
                    or _script_char_text(chars[bridge]) in _PDF_SCRIPT_SIGN_CHARS
                    or _script_char_text(chars[bridge]) == "."
                    for bridge in range(start + 1, end)
                ):
                    protected.add(index)
                    changed = True
                    break
    return protected


def _refine_math_script_tokens(
    chars: list[dict[str, Any]],
    roles: list[ScriptRole],
    tight_bboxes: dict[int, BBox],
    origins: dict[int, tuple[float, float]],
    *,
    formula_region: bool,
    ordinary_native_roles: bool = False,
) -> list[ScriptRole]:
    """Protect base with the leftmost stable cluster, and conservatively reject weak single characters and complex unsegmented token."""
    refined = list(roles)
    # The original rule only has marked seeds to close/refine new scripts; the entire body lines of ordinary native batches do not need to be rescanned for tokens.
    if ordinary_native_roles and all(role == "body" for role in refined):
        return refined
    citation_indices = _citation_script_indices(chars, refined)
    numeric_indices: set[int] = set()
    if any(role == "sup" and _script_char_text(char).isdecimal() for char, role in zip(chars, roles)):
        # The base character of the numeric reference is outside token; double bbox/origin is protected to confirm the entire string and the first digit cannot be reset.
        candidates = _numeric_superscript_indices(build_script_features(chars, tight_bboxes, origins, set()), roles, set())
        numeric_indices = {index for index in candidates if roles[index] == "sup"}
    complex_unsegmented_token = False
    tokens = _iter_math_script_tokens(chars)
    token_alnum_positions = {
        tuple(token): [index for index in token if _is_math_identifier_char(_script_char_text(chars[index]))]
        for token in tokens
    }
    token_splits = {
        tuple(token): _token_split_position(
            chars,
            token,
            refined,
            tight_bboxes,
            origins,
        )
        for token in tokens
    }
    token_families: dict[str, list[tuple[int, ...]]] = {}
    for token in tokens:
        key = tuple(token)
        alnum_positions = token_alnum_positions[key]
        if len(alnum_positions) >= 2:
            token_families.setdefault(_script_char_text(chars[alnum_positions[0]]), []).append(key)
    trusted_family_bases = {
        base
        for base, members in token_families.items()
        if len(members) >= 3
        or any(token_splits[member] is not None for member in members)
        or any(any(_script_char_text(chars[index]) in _PDF_SCRIPT_TOKEN_CONNECTORS for index in member) for member in members)
    }
    for token in tokens:
        if any(index in citation_indices for index in token):
            continue
        if all(index in numeric_indices for index in token):
            # Only the base reselection of purely numerical references is skipped, and subsequent fractional and complex mathematical filtering is still performed according to the original rules.
            continue
        token_key = tuple(token)
        alnum_positions = token_alnum_positions[token_key]
        if not alnum_positions or not any(refined[index] != "body" for index in token):
            continue
        token_roles = {refined[index] for index in token if refined[index] != "body"}
        if token_roles == {"sup", "sub"}:
            complex_unsegmented_token = True
        if len(alnum_positions) == 1:
            continue
        first_position = alnum_positions[0]
        suffix_positions = alnum_positions[1:]
        if (
            refined[first_position] == "sup"
            and all(refined[index] == "body" for index in suffix_positions)
            and len(suffix_positions) >= 2
            and all(_script_char_text(chars[index]).isalpha() for index in suffix_positions)
        ):
            continue
        split_position = token_splits[token_key]
        if split_position is None and _script_char_text(chars[alnum_positions[0]]) in trusted_family_bases:
            split_position = alnum_positions[1]
        if split_position is None:
            if all(refined[index] != "body" for index in alnum_positions):
                for index in token:
                    refined[index] = "body"
            continue
        base_positions = [index for index in alnum_positions if index < split_position]
        base_origins = [origin for index in base_positions if (origin := _token_origin(chars[index], origins)) is not None]
        base_heights = [_token_tight_height(chars[index], tight_bboxes) for index in base_positions]
        base_origin = statistics.median(base_origins) if base_origins else None
        base_height = statistics.median([height for height in base_heights if height > 0]) if any(base_heights) else 0.0
        for index in token:
            if index < split_position or _script_char_text(chars[index]) in _PDF_SCRIPT_TOKEN_CONNECTORS:
                refined[index] = "body"
                continue
            text = _script_char_text(chars[index])
            if not _is_math_identifier_char(text) or refined[index] != "body":
                continue
            origin = _token_origin(chars[index], origins)
            if base_origin is None or origin is None:
                continue
            shift = origin - base_origin
            if abs(shift) >= max(0.35, 0.08 * base_height):
                refined[index] = "sub" if shift > 0 else "sup"
    _close_spaced_script_operators(
        chars,
        refined,
        tight_bboxes,
        origins,
    )
    if complex_unsegmented_token and not formula_region:
        for index in range(len(refined)):
            if index not in citation_indices:
                refined[index] = "body"
    scripted_alnum = [
        index for index, role in enumerate(refined) if role != "body" and _script_char_text(chars[index]).isalnum()
    ]
    if not formula_region and len(scripted_alnum) >= 2 and any(_script_char_text(char) in {"∑", "∫"} for char in chars):
        for index in range(len(refined)):
            if index not in citation_indices:
                refined[index] = "body"
    has_compact_multiply = any(
        _script_char_text(char) == "×"
        and 0 < index < len(chars) - 1
        and not _script_char_text(chars[index - 1]).isspace()
        and not _script_char_text(chars[index + 1]).isspace()
        for index, char in enumerate(chars)
    )
    if not formula_region and len(scripted_alnum) >= 2 and has_compact_multiply:
        for index in range(len(refined)):
            if index not in citation_indices:
                refined[index] = "body"
    if not formula_region:
        for operator_index, char in enumerate(chars):
            operator = _script_char_text(char)
            nearby = [candidate for candidate in scripted_alnum if abs(candidate - operator_index) <= 5]
            if (
                operator in {"/", "⁄"}
                and any(candidate < operator_index for candidate in nearby)
                and any(candidate > operator_index for candidate in nearby)
            ):
                for candidate in nearby:
                    if candidate not in citation_indices:
                        refined[candidate] = "body"
    protected_subscripts = _protected_subscript_indices(
        chars,
        refined,
        tokens,
        tight_bboxes,
        origins,
    )
    for index, role in enumerate(list(refined)):
        if (
            role == "sub"
            and index not in citation_indices
            and index not in protected_subscripts
            and _is_math_identifier_char(_script_char_text(chars[index]))
        ):
            refined[index] = "body"
    for index, role in enumerate(list(refined)):
        if role == "body" or index in citation_indices:
            continue
        text = _script_char_text(chars[index])
        if text.isalnum() or text in _PDF_SCRIPT_AUTHOR_MARKS:
            continue
        if text in {",", "，"} and all(
            0 <= neighbor < len(refined)
            and refined[neighbor] == role
            and (_script_char_text(chars[neighbor]).isalnum() or _script_char_text(chars[neighbor]) in _PDF_SCRIPT_AUTHOR_MARKS)
            for neighbor in (index - 1, index + 1)
        ):
            continue
        if text == "." and all(
            0 <= neighbor < len(refined) and refined[neighbor] == role and _script_char_text(chars[neighbor]).isdigit()
            for neighbor in (index - 1, index + 1)
        ):
            continue
        if text in _PDF_SCRIPT_SIGN_CHARS:
            sign_neighbors = [
                neighbor
                for step in (-1, 1)
                if (neighbor := _nearest_nonspace_index(chars, index, step)) is not None
                and abs(neighbor - index) <= 3
                and refined[neighbor] == role
            ]
            if sign_neighbors and any(_script_char_text(chars[neighbor]).isdigit() for neighbor in sign_neighbors):
                continue
        if text in _PDF_SCRIPT_TRAILING_MARKS:
            previous = _nearest_nonspace_index(chars, index, -1)
            body_prefix = previous - 1 if previous is not None else -1
            if (
                role == "sup"
                and previous is not None
                and index - previous == 1
                and body_prefix >= 0
                and refined[body_prefix] == "body"
                and _script_char_text(chars[body_prefix]).isalpha()
                and refined[previous] == role
                and roles[index] == role
                and _script_char_text(chars[previous]).isalpha()
                and _script_geometry_is_aligned(chars, previous, index, tight_bboxes, origins)
            ):
                continue
        refined[index] = "body"
    if not formula_region:
        _close_compact_aligned_script_suffixes(
            chars,
            roles,
            refined,
            tight_bboxes,
            origins,
        )
    return refined


def _bbox_axis_overlap(first: BBox, second: BBox, *, axis: Literal["x", "y"]) -> float:
    """Returns the absolute overlap length of two bboxs on the specified axis."""
    start, end = (0, 2) if axis == "x" else (1, 3)
    return max(0.0, min(first[end], second[end]) - max(first[start], second[start]))


def _fraction_member_indices(
    page_size: tuple[float, float],
    all_chars: list[dict[str, Any]],
    tight_bboxes: dict[int, BBox],
    drawing_lines: Sequence[Any],
    angle: int,
    prepared_rules: list[BBox | None] | None = None,
) -> set[int]:
    """Recognize the upper and lower characters on both sides of the fraction line at a time according to the page direction, so that complex fractions can be rejected as a whole block."""
    if not all_chars or not drawing_lines:
        return set()
    local_chars: list[tuple[int, str, BBox]] = []
    local_heights = []
    for char in all_chars:
        char_idx = char.get("char_idx")
        text = _script_char_text(char)
        bbox = tight_bboxes.get(char_idx) if isinstance(char_idx, int) else None
        if not isinstance(char_idx, int) or bbox is None or not text.isprintable() or text.isspace():
            continue
        local_bbox = _rotate_bbox_to_upright(bbox, page_size, angle)
        local_chars.append((char_idx, text, local_bbox))
        local_heights.append(local_bbox[3] - local_bbox[1])
    scale = statistics.median([height for height in local_heights if height > 0]) if local_heights else 8.0
    members: set[int] = set()
    indexed = _FractionCharIndex(local_chars) if len(local_chars) >= 32 and len(drawing_lines) >= 4 else None
    for rule_index, drawing in enumerate(drawing_lines):
        if prepared_rules is None:
            raw_bbox = _coerce_bbox(getattr(drawing, "bbox", drawing))
            local_rule = _rotate_bbox_to_upright(raw_bbox, page_size, angle) if raw_bbox is not None else None
        else:
            local_rule = prepared_rules[rule_index]
        if local_rule is None:
            continue
        width = local_rule[2] - local_rule[0]
        height = local_rule[3] - local_rule[1]
        if width < max(2.0, 0.45 * scale) or width > 12.0 * scale or height > max(1.25, 0.25 * scale) or width > 8.0 * scale:
            continue
        rule_y = (local_rule[1] + local_rule[3]) / 2
        aligned = [
            (char_idx, bbox)
            for char_idx, text, bbox in (indexed.query(local_rule, scale) if indexed is not None else local_chars)
            if text.isalnum()
            and abs((bbox[1] + bbox[3]) / 2 - rule_y) <= 2.25 * scale
            and (
                _bbox_axis_overlap(bbox, local_rule, axis="x") > 0
                or local_rule[0] - 0.25 * scale <= (bbox[0] + bbox[2]) / 2 <= local_rule[2] + 0.25 * scale
            )
        ]
        above = [
            (char_idx, bbox)
            for char_idx, bbox in aligned
            if bbox[3] <= rule_y + 0.2 * scale and rule_y - bbox[3] <= 1.75 * scale
        ]
        below = [
            (char_idx, bbox)
            for char_idx, bbox in aligned
            if bbox[1] >= rule_y - 0.2 * scale and bbox[1] - rule_y <= 1.75 * scale
        ]
        if above and below:
            members.update(char_idx for char_idx, _bbox in above)
            members.update(char_idx for char_idx, _bbox in below)
    return members


def _prepare_fraction_rules(drawing_lines: Sequence[Any], page_size: tuple[float, float], angle: int) -> list[BBox | None]:
    """Prepare a partial horizontal line frame according to the original drawing order for reuse by multiple cells in the same table."""

    return [
        _rotate_bbox_to_upright(raw, page_size, angle)
        if (raw := _coerce_bbox(getattr(drawing, "bbox", drawing))) is not None
        else None
        for drawing in drawing_lines
    ]


class _FractionCharIndex:
    """Only the character index of this fractional detection is saved, and the query returns a safe superset of the original character sequence."""

    def __init__(self, chars: list[tuple[int, str, BBox]]):
        """No index will be created for abnormal values, and the query will return to the original list."""

        self.chars = chars
        self.safe = all(type(value) is float and math.isfinite(value) for _index, _text, box in chars for value in box)
        if not self.safe:
            return
        self.order = sorted(range(len(chars)), key=lambda index: (chars[index][2][0], index))
        self.starts = [chars[index][2][0] for index in self.order]
        self.width = 1 << max(0, (len(chars) - 1).bit_length())
        self.maxima = [float("-inf")] * (2 * self.width)
        for position, index in enumerate(self.order):
            self.maxima[self.width + position] = chars[index][2][2]
        for node in range(self.width - 1, 0, -1):
            self.maxima[node] = max(self.maxima[node * 2], self.maxima[node * 2 + 1])

    def query(self, rule: BBox, scale: float) -> list[tuple[int, str, BBox]]:
        """Return characters in a closed range that may intersect laterally, retaining the original order and boundary equality values."""

        if not self.safe or not all(type(value) is float and math.isfinite(value) for value in (*rule, scale)):
            return self.chars
        lower, upper = rule[0] - 0.25 * scale, rule[2] + 0.25 * scale
        if not math.isfinite(lower) or not math.isfinite(upper):
            return self.chars
        end = bisect_right(self.starts, upper)
        stack = [(1, 0, self.width)]
        matches = []
        while stack:
            node, left, right = stack.pop()
            if left >= end or self.maxima[node] < lower:
                continue
            if right - left == 1:
                matches.append(self.order[left])
            else:
                middle = (left + right) // 2
                stack.append((node * 2 + 1, middle, right))
                stack.append((node * 2, left, middle))
        return [self.chars[index] for index in sorted(matches)]


def _strong_structural_script_roles(
    chars: list[dict[str, Any]],
    tight_bboxes: dict[int, BBox],
    origins: dict[int, tuple[float, float]],
) -> dict[int, ScriptRole]:
    """Extract references and adjacencies base strong script evidence that can be preserved in the recovery formula area."""

    roles = classify_char_script_roles(
        chars,
        tight_bboxes=tight_bboxes,
        origins=origins,
    )
    strong_roles: dict[int, ScriptRole] = paired_script_roles(chars, roles, tight_bboxes, origins)
    for index in _citation_script_indices(chars, roles):
        if roles[index] != "body":
            strong_roles[index] = roles[index]
    for index, role in enumerate(roles):
        if role != "sup" or not _is_math_identifier_char(_script_char_text(chars[index])):
            continue
        base_height = _token_tight_height(chars[index - 1], tight_bboxes) if index > 0 else 0.0
        script_height = _token_tight_height(chars[index], tight_bboxes)
        if base_height <= 0 or script_height > 0.8 * base_height:
            continue
        next_index = _nearest_nonspace_index(chars, index, 1)
        if next_index is not None and _is_math_script_token_char(_script_char_text(chars[next_index])):
            continue
        if _has_adjacent_math_base(
            chars,
            index,
            roles,
            tight_bboxes,
            origins,
        ):
            strong_roles[index] = role
    return strong_roles


def _drop_cap_body_indices(
    chars: list[dict[str, Any]],
    tight_bboxes: dict[int, BBox],
    origins: dict[int, tuple[float, float]],
) -> set[int]:
    """Identify lowercase word endings with the same baseline next to the dropped capital letter to avoid mistakenly using the first word of the text as a mathematical superscript."""
    word = []
    for index, char in enumerate(chars):
        text = _script_char_text(char)
        if not text.isascii() or not text.isalpha():
            break
        word.append(index)
    if not 3 <= len(word) <= 12:
        return set()
    if not _script_char_text(chars[0]).isupper() or not all(_script_char_text(chars[index]).islower() for index in word[1:]):
        return set()
    boxes = [tight_bboxes.get(chars[index].get("char_idx")) for index in word]
    points = [origins.get(chars[index].get("char_idx")) for index in word]
    if any(value is None for value in boxes + points):
        return set()
    cap = boxes[0]
    suffix_height = statistics.median(box[3] - box[1] for box in boxes[1:])
    cap_height = cap[3] - cap[1]
    baseline = statistics.median(point[1] for point in points[1:])
    if suffix_height <= 0 or cap_height < 3 * suffix_height:
        return set()
    if not (
        cap[1] + 0.25 * cap_height <= baseline <= cap[1] + 0.7 * cap_height
        and points[0][1] - baseline >= 1.2 * suffix_height
        and -0.1 * suffix_height <= boxes[1][0] - cap[2] <= 1.5 * suffix_height
        and all(abs(point[1] - baseline) <= 0.15 * suffix_height for point in points[1:])
        and all(0.5 * suffix_height <= box[3] - box[1] <= 1.5 * suffix_height for box in boxes[1:])
        and all(box[0] >= cap[2] - 0.1 * suffix_height for box in boxes[1:])
    ):
        return set()
    return set(word)


def _classify_script_runs(
    chars: list[dict[str, Any]],
    local_tight_bboxes: dict[int, BBox],
    local_origins: dict[int, tuple[float, float]],
    memberships: list[int | None],
    prepared_native: tuple | None = None,
    preclassified_native: list[bytes | None] | None = None,
) -> tuple[list[str], list[int], list[bool]]:
    """Classified according to the formula area boundary segmentation, and requires stable body to exist inside the formula segment."""
    roles: list[ScriptRole] = ["body"] * len(chars)
    body_counts = [0] * len(chars)
    formula_flags = [False] * len(chars)
    native_roles = preclassified_native
    if native_roles is None and prepared_native is not None:
        from ....._compute_backend import get_native

        native = get_native()
        if native is not None:
            offsets = [0]
            for index in range(1, len(chars)):
                if memberships[index] != memberships[index - 1]:
                    offsets.append(index)
            offsets.append(len(chars))
            native_roles = native.script_roles_raw_batch(*prepared_native, offsets, _coerce_finite_bbox)
    start = 0
    native_index = 0
    while start < len(chars):
        membership = memberships[start]
        end = start + 1
        while end < len(chars) and memberships[end] == membership:
            end += 1
        run_chars = chars[start:end]
        ordinary_maps = (
            type(local_tight_bboxes) is dict
            and type(local_origins) is dict
            and all(type(char) is dict and type(char.get("char_idx")) is int for char in run_chars)
        )
        run_indices = (
            set() if ordinary_maps else {int(char["char_idx"]) for char in run_chars if isinstance(char.get("char_idx"), int)}
        )
        classified = native_roles[native_index] if native_roles is not None else None
        native_index += 1
        if classified is None:
            run_roles = classify_char_script_roles(
                run_chars,
                tight_bboxes=local_tight_bboxes
                if ordinary_maps
                else {index: local_tight_bboxes[index] for index in run_indices if index in local_tight_bboxes},
                origins=local_origins
                if ordinary_maps
                else {index: local_origins[index] for index in run_indices if index in local_origins},
            )
        else:
            names = ("body", "sup", "sub")
            run_roles = [names[role] for role in classified]
        run_roles = _refine_math_script_tokens(
            run_chars,
            run_roles,
            local_tight_bboxes,
            local_origins,
            formula_region=membership is not None,
            ordinary_native_roles=classified is not None and (prepared_native is not None or preclassified_native is not None),
        )
        if membership is None and any(role != "body" for role in run_roles):
            # The formula members maintain their original script roles; the dropped initials of the text need to be restored together before counting, and the paths of Python and native are shared.
            for index in _drop_cap_body_indices(run_chars, local_tight_bboxes, local_origins):
                run_roles[index] = "body"
        if classified is not None and all(role == "body" for role in run_roles):
            body_count = 0
            for char in run_chars:
                text = _script_char_text(char)
                if text.isprintable() and not text.isspace() and text.isalnum():
                    body_count += 1
            for offset in range(start, end):
                roles[offset] = "body"
                body_counts[offset] = body_count
                formula_flags[offset] = membership is not None
            start = end
            continue
        visible = [
            index
            for index, char in enumerate(run_chars)
            if str(char.get("char", "")).isprintable() and not str(char.get("char", "")).isspace()
        ]
        body_count = sum(run_roles[index] == "body" and str(run_chars[index].get("char", "")).isalnum() for index in visible)
        marked_count = sum(run_roles[index] != "body" for index in visible)
        body_tight_heights = [
            local_tight_bboxes[int(run_chars[index]["char_idx"])][3] - local_tight_bboxes[int(run_chars[index]["char_idx"])][1]
            for index in visible
            if run_roles[index] == "body"
            and isinstance(run_chars[index].get("char_idx"), int)
            and int(run_chars[index]["char_idx"]) in local_tight_bboxes
        ]
        script_tight_heights = [
            local_tight_bboxes[int(run_chars[index]["char_idx"])][3] - local_tight_bboxes[int(run_chars[index]["char_idx"])][1]
            for index in visible
            if run_roles[index] != "body"
            and isinstance(run_chars[index].get("char_idx"), int)
            and int(run_chars[index]["char_idx"]) in local_tight_bboxes
        ]
        stable_formula_body = (
            body_count > 0
            and bool(body_tight_heights)
            and (not script_tight_heights or max(body_tight_heights) >= 1.1 * max(script_tight_heights))
        )
        if membership is not None and (not stable_formula_body or marked_count >= len(visible)):
            run_roles = ["body"] * len(run_chars)
        for offset, role in enumerate(run_roles, start=start):
            roles[offset] = role
            body_counts[offset] = body_count
            formula_flags[offset] = membership is not None
        start = end
    return roles, body_counts, formula_flags


def _plain_script_geometry(chars, tight_bboxes, origins):
    """Only ordinary geometries without custom read behavior can be borrowed directly, and special values retain their original character-by-character materialization."""
    if type(tight_bboxes) is not dict or type(origins) is not dict:
        return False
    for char in chars:
        if type(char) is not dict or type(char.get("char")) is not str or type(char.get("char_idx")) is not int:
            return False
        box = char.get("bbox")
        raw = box.bbox if type(box) is CharBox else box
        for value, size in ((raw, 4), (tight_bboxes.get(char["char_idx"]), 4), (origins.get(char["char_idx"]), 2)):
            if value is not None and (
                type(value) not in (tuple, list)
                or len(value) != size
                or any(type(v) is not float or not math.isfinite(v) for v in value)
            ):
                return False
    return True


def _prepare_plain_script_input(chars, tight_bboxes, origins):
    """Packs a line of read-only script input while validating normal characters; special objects return a reference path."""

    if type(tight_bboxes) is not dict or type(origins) is not dict:
        return None
    loose, tight, points, flags, font_ids = [], [], [], [], []
    fonts = {}
    for char in chars:
        if type(char) is not dict or type(char.get("char")) is not str or type(char.get("char_idx")) is not int:
            return None
        index = char["char_idx"]
        box = char.get("bbox")
        raw = box.bbox if type(box) is CharBox else box
        tight_box = tight_bboxes.get(index)
        point = origins.get(index)
        for value, size in ((raw, 4), (tight_box, 4), (point, 2)):
            if value is not None and (
                type(value) not in (tuple, list)
                or len(value) != size
                or any(type(item) is not float or not math.isfinite(item) for item in value)
            ):
                return None
        font = char.get("font")
        if font is not None and (
            type(font) is not dict
            or any(type(font.get(key)) not in (str, int, float, bool, type(None)) for key in ("name", "flags", "weight"))
        ):
            return None
        font_key = _script_font_key(char)
        loose.append(raw)
        tight.append(tight_box)
        points.append(point)
        flags.append(_native_text_flags(char["char"]))
        font_ids.append(-1 if font_key is None else fonts.setdefault(font_key, len(fonts)))
    return loose, tight, points, flags, font_ids


def _pack_plain_script_input(chars, tight_bboxes, origins):
    """Only ordinary container shapes are checked, and built-in floating point verification of coordinates is performed by the batch Rust entry."""

    if type(tight_bboxes) is not dict or type(origins) is not dict:
        return None
    loose, tight, points, flags, font_ids = [], [], [], [], []
    fonts = {}
    for char in chars:
        if type(char) is not dict or type(char.get("char")) is not str or type(char.get("char_idx")) is not int:
            return None
        box = char.get("bbox")
        if type(box) is CharBox:
            if type(box.bbox) is not list:
                return None
            box = box.bbox
        elif box is not None and type(box) not in (tuple, list):
            return None
        font = char.get("font")
        if font is not None and (
            type(font) is not dict
            or any(type(font.get(key)) not in (str, int, float, bool, type(None)) for key in ("name", "flags", "weight"))
        ):
            return None
        key = _script_font_key(char)
        index = char["char_idx"]
        loose.append(box)
        tight.append(tight_bboxes.get(index))
        points.append(origins.get(index))
        flags.append(_native_text_flags(char["char"]))
        font_ids.append(-1 if key is None else fonts.setdefault(key, len(fonts)))
    return loose, tight, points, flags, font_ids


def _script_line_char_roles(
    line: Any,
    page_size: tuple[float, float],
    tight_bboxes: dict[int, BBox],
    origins: dict[int, tuple[float, float]],
    fraction_members: set[int],
    prepared: tuple[list[dict[str, Any]], list[int | None], list[bytes | None]] | None = None,
) -> tuple[list[dict[str, Any]], list[ScriptRole], list[int], list[bool]]:
    """Return the original characters and their superscript and subscript roles in segments according to the same formula in the text."""

    chars = prepared[0] if prepared is not None else _ordered_line_chars(line)
    if not chars:
        return [], [], [], []
    angle = int(getattr(line, "angle", 0) or 0) % 360
    local_chars: list[dict[str, Any]] = []
    local_tight_bboxes: dict[int, BBox] = {}
    local_origins: dict[int, tuple[float, float]] = {}
    from ....._compute_backend import get_native

    native = get_native()
    packed = (
        _prepare_plain_script_input(chars, tight_bboxes, origins)
        if native is not None and angle == 0 and prepared is None
        else None
    )
    reuse_geometry = angle == 0 and (
        prepared is not None or packed is not None or _plain_script_geometry(chars, tight_bboxes, origins)
    )
    normalized_boxes = (
        native.normalize_boxes([getattr(char.get("bbox"), "bbox", char.get("bbox")) for char in chars], True, _coerce_bbox)
        if native is not None and not reuse_geometry
        else None
    )
    if reuse_geometry:
        local_chars, local_tight_bboxes, local_origins = chars, tight_bboxes, origins
    for position, char in enumerate(() if reuse_geometry else chars):
        local_char = dict(char)
        bbox = normalized_boxes[position] if normalized_boxes is not None else _coerce_bbox(char.get("bbox"))
        if bbox is not None:
            local_char["bbox"] = _rotate_bbox_to_upright(bbox, page_size, angle)
        local_chars.append(local_char)
        char_idx = char.get("char_idx")
        if not isinstance(char_idx, int):
            continue
        tight_bbox = tight_bboxes.get(char_idx)
        if tight_bbox is not None:
            local_tight_bboxes[char_idx] = _rotate_bbox_to_upright(
                tight_bbox,
                page_size,
                angle,
            )
        origin = origins.get(char_idx)
        if origin is not None:
            local_origins[char_idx] = _rotate_origin_to_upright(
                origin,
                page_size,
                angle,
            )
    regions = [bbox for value in getattr(line, "inline_math_regions", []) if (bbox := _coerce_bbox(value)) is not None]
    memberships = prepared[1] if prepared is not None else _script_region_memberships(chars, tight_bboxes, regions)
    roles, body_counts, formula_flags = _classify_script_runs(
        local_chars,
        local_tight_bboxes,
        local_origins,
        memberships,
        packed,
        prepared[2] if prepared is not None else None,
    )
    if bool(getattr(line, "compact_formula_cluster", False)) or (
        bool(getattr(line, "restored_inline_cluster", False)) and bool(regions)
    ):
        strong_structural_roles = _strong_structural_script_roles(
            local_chars,
            local_tight_bboxes,
            local_origins,
        )
        roles = [strong_structural_roles.get(index, "body") for index in range(len(roles))]
    for index, char in enumerate(chars):
        char_idx = char.get("char_idx")
        if isinstance(char_idx, int) and char_idx in fraction_members:
            roles[index] = "body"
    return chars, roles, body_counts, formula_flags


def _script_line_payload(
    line: Any,
    page_size: tuple[float, float],
    tight_bboxes: dict[int, BBox],
    origins: dict[int, tuple[float, float]],
    fraction_members: set[int],
    prepared: tuple[list[dict[str, Any]], list[int | None], list[bytes | None]] | None = None,
) -> PDFTextScriptLine | None:
    """Converts the Flash line into the formula segmented compact superscript sidecar."""

    chars, roles, body_counts, formula_flags = _script_line_char_roles(
        line,
        page_size,
        tight_bboxes,
        origins,
        fraction_members,
        prepared,
    )
    if not chars:
        return None
    angle = int(getattr(line, "angle", 0) or 0) % 360
    if (
        not any(role != "body" for role in roles)
        and type(tight_bboxes) is dict
        and all(type(char) is dict and type(char.get("char_idx")) is int for char in chars)
    ):
        return _plain_script_payload(line, chars, angle)
    compact_parts: list[str] = []
    compact_roles: list[str] = []
    compact_bboxes: list[BBox | None] = []
    compact_body_counts: list[int] = []
    compact_formula_flags: list[bool] = []
    for index, char in enumerate(chars):
        fragment = _normalize_match_fragment(char.get("char"))
        if not fragment:
            continue
        compact_parts.append(fragment)
        char_idx = char.get("char_idx")
        page_tight_bbox = tight_bboxes.get(char_idx) if isinstance(char_idx, int) else None
        compact_roles.extend([roles[index]] * len(fragment))
        compact_bboxes.extend([page_tight_bbox] * len(fragment))
        compact_body_counts.extend([body_counts[index]] * len(fragment))
        compact_formula_flags.extend([formula_flags[index]] * len(fragment))
    text = "".join(compact_parts)
    if not text:
        return None
    ranges: list[PDFTextScriptRange] = []
    start = 0
    while start < len(compact_roles):
        role = compact_roles[start]
        end = start + 1
        while end < len(compact_roles) and compact_roles[end] == role:
            end += 1
        if role in {"sup", "sub"}:
            range_bboxes = [bbox for bbox in compact_bboxes[start:end] if bbox is not None]
            if range_bboxes:
                page_bbox = (
                    min(bbox[0] for bbox in range_bboxes),
                    min(bbox[1] for bbox in range_bboxes),
                    max(bbox[2] for bbox in range_bboxes),
                    max(bbox[3] for bbox in range_bboxes),
                )
                ranges.append(
                    PDFTextScriptRange(
                        start=start,
                        end=end,
                        style="superscript" if role == "sup" else "subscript",
                        bbox=page_bbox,
                        stable_body_count=max(compact_body_counts[start:end], default=0),
                        formula_region=any(compact_formula_flags[start:end]),
                    )
                )
        start = end
    return PDFTextScriptLine(
        bbox=getattr(line, "bbox"),
        text=text,
        script_ranges=tuple(ranges),
        source_index=int(getattr(line, "source_index", 0) or 0),
        angle=angle,
    )


def _plain_script_payload(line: Any, chars: list[dict[str, Any]], angle: int) -> PDFTextScriptLine | None:
    """The full text line only constructs the original text sidecar, and does not copy the boxes that will not participate in the range calculation."""

    text = "".join(fragment for char in chars if (fragment := _normalize_match_fragment(char.get("char"))))
    if not text:
        return None
    return PDFTextScriptLine(
        bbox=getattr(line, "bbox"),
        text=text,
        script_ranges=(),
        source_index=int(getattr(line, "source_index", 0) or 0),
        angle=angle,
    )


def _prepare_owned_script_evidence(owner, chars):
    """For use only by homologous geometries that have not yet been exposed; Python identity or variable character content across calls is not cached."""
    if _native_text_flags is not _STANDARD_NATIVE_TEXT_FLAGS or _script_font_key is not _STANDARD_SCRIPT_FONT_KEY:
        return None
    prepared = owner.prepare_script_evidence(_native_text_flags)
    if prepared is None:
        return None
    return prepared, {id(char): index for index, char in enumerate(chars)}


def detect_pdf_text_script_lines(
    lines: list[Any],
    page_size: tuple[float, float],
    tight_bboxes: dict[int, BBox],
    origins: dict[int, tuple[float, float]],
    *,
    all_chars: list[dict[str, Any]] | None = None,
    drawing_lines: Sequence[Any] | None = None,
    _owned_inputs=None,
) -> list[PDFTextScriptLine]:
    """Detecting superscript and subscript candidates in Flash remaining natural text lines."""
    resolved_chars = all_chars or []
    resolved_drawings = drawing_lines or []
    if type(lines) is list and not lines:
        # Empty pages retain backend loading checks and parameter truth values, and do not create classification closures that will not be consumed.
        from ....._compute_backend import get_native

        get_native()
        return []
    fraction_members_by_angle = {
        angle: _fraction_member_indices(
            page_size,
            resolved_chars,
            tight_bboxes,
            resolved_drawings,
            angle,
        )
        for angle in {int(getattr(line, "angle", 0) or 0) % 360 for line in lines}
    }
    from ....._compute_backend import get_native

    native = get_native()
    output: list[PDFTextScriptLine] = []
    pending = []
    pending_chars = 0

    def append_line(line: Any, prepared: tuple | None = None) -> None:
        """Materialize a piece of evidence in original line order and release this batch of character references."""

        angle = int(getattr(line, "angle", 0) or 0) % 360
        payload = _script_line_payload(line, page_size, tight_bboxes, origins, fraction_members_by_angle[angle], prepared)
        if payload is not None:
            output.append(payload)

    def flush_pending() -> None:
        """Each batch of independent lines can have a maximum of 64 entries or 8192 words, and extra-long lines will be counted separately."""

        nonlocal pending_chars
        if not pending:
            return
        loose, tight, points, flags, font_ids = [], [], [], [], []
        offsets = [0]
        records = []
        owned = type(pending[0][3]) is list
        for line, chars, memberships, packed in pending:
            start = len(offsets) - 1
            if owned:
                loose.extend(packed)
            else:
                for source, target in zip((loose, tight, points, flags, font_ids), packed, strict=True):
                    source.extend(target)
            for position in range(1, len(chars)):
                if memberships[position] != memberships[position - 1]:
                    offsets.append(len(loose) - len(chars) + position)
            offsets.append(len(loose))
            records.append((line, chars, memberships, start, len(offsets) - 1))
        roles = (
            _owned_inputs[0].classify_indices(loose, offsets)
            if owned
            else native.script_roles_plain_batch(loose, tight, points, flags, font_ids, offsets, _coerce_finite_bbox)
        )
        if roles is None:
            for line, _chars, _memberships, _first, _last in records:
                append_line(line)
            pending.clear()
            pending_chars = 0
            return
        for line, chars, memberships, first, last in records:
            classified = roles[first:last]
            if (
                all(item is not None and all(role == 0 for role in item) for item in classified)
                and not bool(getattr(line, "compact_formula_cluster", False))
                and not bool(getattr(line, "restored_inline_cluster", False))
            ):
                payload = _plain_script_payload(line, chars, 0)
                if payload is not None:
                    output.append(payload)
            else:
                append_line(line, (chars, memberships, classified))
        pending.clear()
        pending_chars = 0

    for line in lines:
        if native is not None and int(getattr(line, "angle", 0) or 0) % 360 == 0:
            chars = _ordered_line_chars(line)
            packed = None
            if chars and _owned_inputs is not None:
                indices = [_owned_inputs[1].get(id(char)) for char in chars]
                if all(index is not None for index in indices):
                    packed = indices
            if packed is None:
                packed = _pack_plain_script_input(chars, tight_bboxes, origins) if chars else None
            if packed is not None:
                regions = [
                    bbox for value in getattr(line, "inline_math_regions", []) if (bbox := _coerce_bbox(value)) is not None
                ]
                memberships = _script_region_memberships(chars, tight_bboxes, regions)
                if pending and (
                    len(pending) >= 64
                    or pending_chars + len(chars) > 8192
                    or (type(pending[0][3]) is list) != (type(packed) is list)
                ):
                    flush_pending()
                pending.append((line, chars, memberships, packed))
                pending_chars += len(chars)
                if len(chars) > 8192:
                    flush_pending()
                continue
        flush_pending()
        append_line(line)
    flush_pending()
    return output


__all__ = [
    "_rotate_origin_to_upright",
    "_bbox_center_inside_region",
    "_script_region_memberships",
    "_script_char_text",
    "_is_cjk_text",
    "_is_math_identifier_char",
    "_is_math_script_token_char",
    "_iter_math_script_tokens",
    "_citation_script_indices",
    "_token_origin",
    "_token_tight_height",
    "_has_adjacent_math_base",
    "_horizontal_gap_between_bboxes",
    "_token_split_position",
    "_script_geometry_is_aligned",
    "_nearest_nonspace_index",
    "_close_spaced_script_operators",
    "_close_compact_aligned_script_suffixes",
    "_protected_subscript_indices",
    "_refine_math_script_tokens",
    "_bbox_axis_overlap",
    "_fraction_member_indices",
    "_strong_structural_script_roles",
    "_classify_script_runs",
    "_script_line_char_roles",
    "_script_line_payload",
    "detect_pdf_text_script_lines",
]

# Custom character classification rules are still prepared according to the original parameters and do not use the solidified native font key semantics.
_STANDARD_NATIVE_TEXT_FLAGS = _native_text_flags
_STANDARD_SCRIPT_FONT_KEY = _script_font_key
