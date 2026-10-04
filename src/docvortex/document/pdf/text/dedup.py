"""Clean native PDF duplicate text by glyph source, giving priority to content that cannot be proven to be duplicated."""

from __future__ import annotations

import math
import unicodedata
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ._contracts import Bbox, Char
from .geometry import char_bbox_values
from ...._compute_backend import get_native

_GEOMETRY_EPSILON = 0.001
_MAX_OFFSET = 2.5
_TRANSLATION_EPSILON = 0.1
_MIN_OVERLAP = 0.45
_MAX_BUCKET_CANDIDATES = 64
Box = tuple[float, float, float, float]


@dataclass
class _Glyph:
    """Keep a group of characters that may come from the same glyph, and never deduplicate the same letters within the group."""

    chars: list[Char]
    box: Box | None
    text: str
    angle: float

    @property
    def head(self) -> Char:
        """Returns the representative character responsible for geometry and drawing source determination."""
        return self.chars[0]

    @property
    def signature(self) -> tuple[Any, ...]:
        """Repeated draws must have the same text, font, orientation, and rendering."""
        font = self.head.get("font") or {}
        return (
            self.text,
            *(font.get(k) for k in ("name", "flags", "size", "weight")),
            round(self.angle, 3),
            self.head.get("text_render_mode"),
            self.head.get("text_is_visible"),
        )


def _close_values(a: tuple[float, ...] | None, b: tuple[float, ...] | None, tolerance: float) -> bool:
    """Missing or non-finite geometries are not considered evidence of identical locations."""
    return (
        a is not None
        and b is not None
        and len(a) == len(b)
        and all(math.isfinite(x) and math.isfinite(y) and abs(x - y) <= tolerance for x, y in zip(a, b))
    )


def _same_mapping(previous: Char, current: Char) -> bool:
    """Protect ligatures and other one-to-many mappings with continuous source indexing, same objects, and coincident geometries."""
    return (
        previous.get("text_object_id") is not None
        and previous.get("text_object_id") == current.get("text_object_id")
        and max(previous.get("source_indices", (previous["char_idx"],))) + 1 == current["char_idx"]
        and previous["font"] == current["font"]
        and previous["rotation"] == current["rotation"]
        and _close_values(previous.get("origin"), current.get("origin"), _GEOMETRY_EPSILON)
        and _close_values(char_bbox_values(previous["bbox"]), char_bbox_values(current["bbox"]), _GEOMETRY_EPSILON)
        and bool(previous["char"].strip())
        and bool(current["char"].strip())
    )


@lru_cache(maxsize=1)
def _radical_equivalents() -> dict[str, str]:
    """Lazy reading of fixed Unicode data, using only radical intervals and not extending to global text replacement."""
    path = Path(__file__).resolve().parents[3] / "resources/unicode/EquivalentUnifiedIdeograph-17.0.0.txt"
    mapping: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        source, target = (value.strip() for value in entry.split(";"))
        bounds = source.split("..")
        for code in range(int(bounds[0], 16), int(bounds[-1], 16) + 1):
            if 0x2E80 <= code <= 0x2FDF:
                mapping[chr(code)] = chr(int(target, 16))
    return mapping


def _is_han(text: str) -> bool:
    """Recognize only unified Chinese characters and avoid treating letters, symbols, or multi-character text as Chinese character variants."""
    return len(text) == 1 and unicodedata.name(text, "").startswith("CJK UNIFIED IDEOGRAPH-")


def _canonical_han(text: str) -> str:
    """Query radicals or compatible Chinese character equivalence relationships for single characters in local mapping groups."""
    if len(text) != 1:
        return text
    if 0x2E80 <= ord(text) <= 0x2FDF:
        return _radical_equivalents().get(text, text)
    return unicodedata.normalize("NFKC", text)


def _source_indices(chars: list[Char]) -> tuple[int, ...]:
    """Stable merging of original indexes, independent of collection traversal order."""
    return tuple(sorted({i for char in chars for i in char.get("source_indices", (char["char_idx"],))}))


def _mapping_char_groups_python(chars):
    """The original iteration protocol, short-circuit checks, and exception order are kept intact."""
    groups = []
    for char in chars:
        if groups and _same_mapping(groups[-1][-1], char):
            groups[-1].append(char)
        else:
            groups.append([char])
    return groups


def _native_mapping_ranges(chars, native):
    """Only packaged regular characters; font strong references and equivalent classes only exist in this deduplication call."""
    records, font_cache, font_ids = [], {}, {}
    for char in chars:
        if type(char) is not dict or not {"bbox", "font", "char", "rotation", "char_idx"} <= char.keys():
            return None
        index, text, rotation = char["char_idx"], char["char"], char["rotation"]
        obj, source = char.get("text_object_id"), char.get("source_indices", (index,))
        if (
            type(index) is not int
            or not -(2**63) <= index < 2**63
            or (obj is not None and (type(obj) is not int or not -(2**63) <= obj < 2**63))
            or type(text) is not str
            or type(rotation) is not float
            or type(source) is not tuple
            or not source
            or any(type(value) is not int or not -(2**63) <= value < 2**63 for value in source)
        ):
            return None
        box = char["bbox"]
        if (
            type(box) is not Bbox
            or type(box.bbox) is not list
            or len(box.bbox) != 4
            or any(type(v) is not float for v in box.bbox)
        ):
            return None
        origin = char.get("origin")
        if origin is not None and (type(origin) is not tuple or len(origin) != 2 or any(type(v) is not float for v in origin)):
            return None
        font = char["font"]
        cached = font_cache.get(id(font))
        if cached is None:
            if type(font) is not dict or font.keys() != {"name", "flags", "size", "weight"}:
                return None
            values = tuple(font[key] for key in ("name", "flags", "size", "weight"))
            if any(
                type(value) not in (str, int, float, bool, type(None)) or (type(value) is float and not math.isfinite(value))
                for value in values
            ):
                return None
            cached = (font, font_ids.setdefault(values, len(font_ids)))
            font_cache[id(font)] = cached
        records.append((obj, index, max(source), cached[1], rotation, origin, box.bbox, bool(text.strip())))
    return native.mapping_runs(records)


def _mapping_groups(chars: list[Char]) -> list[_Glyph]:
    """Rust directly borrows ordinary characters to complete protection group and frame preparation; special input retains the complete reference path."""
    native = get_native()
    if native is not None and type(chars) is list:
        rows = native.mapping_glyph_rows(chars, Bbox)
        if rows is not None:
            result = []
            for first, last, box, angle in rows:
                group = chars[first:last]
                if last > first + 1 and len({char["char"] for char in group}) > 1:
                    equivalents = {_canonical_han(char["char"]) for char in group}
                    if len(equivalents) == 1 and _is_han(canonical := next(iter(equivalents))):
                        replacement = group[0].copy()
                        replacement["char"] = canonical
                        replacement["source_indices"] = _source_indices(group)
                        group = [replacement]
                text = group[0]["char"] if len(group) == 1 else "".join(char["char"] for char in group)
                result.append(_Glyph(group, box, text, angle))
            return result
    return _mapping_groups_reference(chars)


def _mapping_groups_reference(chars: list[Char]) -> list[_Glyph]:
    """First create a protection group and only merge the abnormal mappings with different codes pointing to the same Chinese character."""
    # In the actual measurement of the full-page entrance, the input packaging cost of native pre-grouping exceeds the calculation benefit; the kernel difference is retained but not enabled by default.
    groups = _mapping_char_groups_python(chars)
    result: list[_Glyph] = []
    for group in groups:
        if len(group) > 1 and len({c["char"] for c in group}) > 1:
            equivalents = {_canonical_han(c["char"]) for c in group}
            if len(equivalents) == 1 and _is_han(canonical := next(iter(equivalents))):
                replacement = group[0].copy()
                replacement["char"] = canonical
                replacement["source_indices"] = _source_indices(group)
                group = [replacement]
        head = group[0]
        box = char_bbox_values(head["bbox"])
        angle = head.get("writing_angle", -head["rotation"])
        if not math.isfinite(angle):
            box = None
        if box is not None and (not all(math.isfinite(v) for v in box) or box[2] <= box[0] or box[3] <= box[1]):
            box = None
        result.append(_Glyph(group, box, "".join(c["char"] for c in group), angle))
    return result


def _overlap(a: Box, b: Box) -> float:
    """Calculate the proportion of the intersection to the area of the smaller frame, and zero area cannot be the basis for deletion."""
    area = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1])) / area if area > 0 else 0.0


def _project(box: Box, angle: float) -> Box:
    """Project the page frame to the text writing axis and handle horizontal and rotated text uniformly."""
    co, si = math.cos(angle), math.sin(angle)
    points = [(co * x + si * y, -si * x + co * y) for x in (box[0], box[2]) for y in (box[1], box[3])]
    return min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)


def _bucket(box: Box, size: float) -> tuple[int, int]:
    """Local duplicate candidates indexed by upper left corner."""
    return math.floor(box[0] / size), math.floor(box[1] / size)


def _paint_pairs(glyphs: list[_Glyph]) -> tuple[list[tuple[int, int]], list[tuple[int, int, float, float]]]:
    """Batch filtering and repeated drawing, abnormal source types and huge coordinates retain reference paths."""
    native = get_native()
    if native is not None:
        signatures, objects, records = {}, {}, []
        for glyph in glyphs:
            head = glyph.head
            obj, origin = head.get("text_object_id"), head.get("origin")
            eligible = (
                glyph.box is not None
                and bool(glyph.text.strip())
                and obj is not None
                and origin is not None
                and head.get("text_render_mode") in (0, 1, 2, 3, 4, 5, 6)
            )
            if eligible:
                if len(origin) != 2:
                    return _paint_pairs_python(glyphs)
                try:
                    object_id = objects.setdefault(obj, len(objects))
                except TypeError:
                    return _paint_pairs_python(glyphs)
                signature = signatures.setdefault(glyph.signature, len(signatures))
                records.append((glyph.box, origin, signature, object_id, True))
            else:
                records.append((None, None, 0, 0, False))
        result = native.paint_pairs(records)
        if result is not None:
            return result
    return _paint_pairs_python(glyphs)


def _paint_pairs_python(glyphs: list[_Glyph]) -> tuple[list[tuple[int, int]], list[tuple[int, int, float, float]]]:
    """Only different text objects are compared, generating exact duplicates and translation candidates to be confirmed by continuous evidence."""
    buckets: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    exact: list[tuple[int, int]] = []
    offsets: list[tuple[int, int, float, float]] = []
    for index, glyph in enumerate(glyphs):
        if (
            glyph.box is None
            or not glyph.text.strip()
            or glyph.head.get("text_object_id") is None
            or glyph.head.get("origin") is None
            or glyph.head.get("text_render_mode") not in (0, 1, 2, 3, 4, 5, 6)
        ):
            continue
        bx, by = _bucket(glyph.box, _MAX_OFFSET)
        signature = glyph.signature
        for x in range(bx - 1, bx + 2):
            for y in range(by - 1, by + 2):
                candidates = buckets.get((signature, x, y), ())
                # Pathological stacking is conservatively preserved, limiting candidates at the same position to avoid degenerating into full-page quadratic comparisons.
                if len(candidates) >= _MAX_BUCKET_CANDIDATES:
                    continue
                for other in candidates:
                    previous = glyphs[other]
                    if previous.head["text_object_id"] == glyph.head["text_object_id"]:
                        continue
                    a, b = previous.box, glyph.box
                    assert a is not None
                    po, co = previous.head["origin"], glyph.head["origin"]
                    assert po is not None and co is not None
                    if _close_values(a, b, _GEOMETRY_EPSILON) and _close_values(po, co, _GEOMETRY_EPSILON):
                        exact.append((other, index))
                        continue
                    dx, dy = co[0] - po[0], co[1] - po[1]
                    if not (math.isfinite(dx) and math.isfinite(dy) and max(abs(dx), abs(dy)) <= _MAX_OFFSET):
                        continue
                    if (
                        all(
                            abs(delta - target) <= _TRANSLATION_EPSILON
                            for delta, target in zip((b[0] - a[0], b[1] - a[1], b[2] - a[2], b[3] - a[3]), (dx, dy, dx, dy))
                        )
                        and _overlap(a, b) >= _MIN_OVERLAP
                    ):
                        offsets.append((other, index, dx, dy))
        key = (signature, bx, by)
        if len(buckets[key]) < _MAX_BUCKET_CANDIDATES:
            buckets[key].append(index)
    return exact, offsets


def _components(count: int, pairs: list[tuple[int, int]]) -> list[int]:
    """Attribute candidate or confirmed draw associations to the earliest source to avoid counting multiple layers of copies as multiple glyphs."""
    native = get_native()
    if native is not None:
        return native.dedup_components(count, pairs)
    return _components_python(count, pairs)


def _components_python(count: int, pairs: list[tuple[int, int]]) -> list[int]:
    """Keep the earliest source and lookup the Python reference implementation."""
    parents = list(range(count))
    for a, b in pairs:
        while parents[a] != a:
            parents[a] = parents[parents[a]]
            a = parents[a]
        while parents[b] != b:
            parents[b] = parents[parents[b]]
            b = parents[b]
        parents[max(a, b)] = min(a, b)
    for index in range(count):
        parents[index] = parents[parents[index]]
    return parents


def _only_endpoint_copies(glyphs: list[_Glyph], roots: list[int], start: int, end: int) -> bool:
    """Only spaces and copies of endpoint glyphs are allowed in the middle of consecutive evidence, and unmatched text cannot be skipped."""
    if end - start > 2 * _MAX_BUCKET_CANDIDATES:
        return False
    endpoints = {roots[start], roots[end]}
    return all(glyphs[i].text.isspace() or roots[i] in endpoints for i in range(start + 1, end))


def _confirmed_offsets(
    glyphs: list[_Glyph], pairs: list[tuple[int, int, float, float]], exact: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Use the Python mathematical function to precalculate the direction, and then confirm the translation evidence of the original sequence in batches."""
    native = get_native()
    if not pairs:
        return []
    if native is not None:
        active = {p[0] for p in pairs}
        texts, angles, records = {}, {}, []
        for index, glyph in enumerate(glyphs):
            text_id = texts.setdefault(glyph.text, len(texts))
            projected, normal, angle_id = None, 0.0, 0
            if index in active:
                origin = glyph.head["origin"]
                normal = -math.sin(glyph.angle) * origin[0] + math.cos(glyph.angle) * origin[1]
                if not math.isfinite(normal) or abs(normal) > 1e15:
                    return _confirmed_offsets_python(glyphs, pairs, exact)
                angle_id = angles.setdefault(round(glyph.angle * 1000), len(angles))
                projected = _project(glyph.box, glyph.angle)
            records.append((projected, normal, angle_id, text_id, glyph.text.isspace()))
        return native.confirmed_offsets(records, pairs, exact)
    return _confirmed_offsets_python(glyphs, pairs, exact)


def _confirmed_offsets_python(
    glyphs: list[_Glyph], pairs: list[tuple[int, int, float, float]], exact: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Confirm shadows with evidence of polyglyphs with the same baseline, same translation, and continuity, and reject isolated near-coinciding characters."""
    if not pairs:
        return []
    roots = _components(len(glyphs), exact + [(p[0], p[1]) for p in pairs])
    clusters: dict[tuple[int, int, int, int], list[list[tuple[int, int, float, float]]]] = defaultdict(list)
    for pair in pairs:
        a, _, dx, dy = pair
        glyph = glyphs[a]
        origin = glyph.head["origin"]
        assert origin is not None
        normal = -math.sin(glyph.angle) * origin[0] + math.cos(glyph.angle) * origin[1]
        key = (
            round(glyph.angle * 1000),
            math.floor(dx / _TRANSLATION_EPSILON),
            math.floor(dy / _TRANSLATION_EPSILON),
            math.floor(normal / _TRANSLATION_EPSILON),
        )
        found = None
        for x in range(key[1] - 1, key[1] + 2):
            for y in range(key[2] - 1, key[2] + 2):
                for n in range(key[3] - 1, key[3] + 2):
                    for cluster in clusters.get((key[0], x, y, n), ()):
                        first = cluster[0]
                        ref = glyphs[first[0]]
                        ro = ref.head["origin"]
                        assert ro is not None
                        rn = -math.sin(ref.angle) * ro[0] + math.cos(ref.angle) * ro[1]
                        if (
                            abs(dx - first[2]) <= _TRANSLATION_EPSILON
                            and abs(dy - first[3]) <= _TRANSLATION_EPSILON
                            and abs(normal - rn) <= _TRANSLATION_EPSILON
                        ):
                            found = cluster
                            break
                    if found is not None:
                        break
                if found is not None:
                    break
            if found is not None:
                break
        if found is None:
            clusters[key].append([pair])
        else:
            found.append(pair)
    confirmed: list[tuple[int, int]] = []
    for cluster_list in clusters.values():
        for cluster in cluster_list:
            ordered = sorted(cluster, key=lambda p: (p[0], p[1]))
            runs: list[list[tuple[int, int, float, float]]] = []
            for pair in ordered:
                if runs:
                    prev = runs[-1][-1]
                    pb = _project(glyphs[prev[0]].box, glyphs[prev[0]].angle)
                    cb = _project(glyphs[pair[0]].box, glyphs[pair[0]].angle)
                    # The gap is equal to at most one normal space and requires both content to advance along the source index.
                    continuous = (
                        pair[0] > prev[0]
                        and pair[1] > prev[1]
                        and cb[0] >= pb[0]
                        and cb[0] - pb[2] <= max(1.0, 0.6 * min(pb[3] - pb[1], cb[3] - cb[1]))
                        and _only_endpoint_copies(glyphs, roots, prev[0], pair[0])
                        and _only_endpoint_copies(glyphs, roots, prev[1], pair[1])
                    )
                else:
                    continuous = False
                if not continuous:
                    runs.append([])
                runs[-1].append(pair)
            for run in runs:
                if len({roots[p[0]] for p in run}) >= 3 and len({glyphs[p[0]].text for p in run}) >= 2:
                    confirmed.extend((p[0], p[1]) for p in run)
    return confirmed


def _merge_sources(retained: _Glyph, duplicate: _Glyph) -> None:
    """Copy the representative characters and then merge the sources to avoid modifying the caller's original character record."""
    if len(retained.chars) == len(duplicate.chars):
        merged = []
        for a, b in zip(retained.chars, duplicate.chars):
            char = a.copy()
            char["source_indices"] = _source_indices([a, b])
            merged.append(char)
        retained.chars = merged
    else:
        retained.chars = [c.copy() for c in retained.chars]
        retained.chars[0]["source_indices"] = _source_indices(retained.chars + duplicate.chars)


def _collapse_paints(glyphs: list[_Glyph]) -> list[_Glyph]:
    """The earliest source is retained according to the connected repetition relationship, and repeated drawings of more than three layers are merged."""
    exact, offsets = _paint_pairs(glyphs)
    parents = _components(len(glyphs), exact + _confirmed_offsets(glyphs, offsets, exact))
    for index in range(len(glyphs) - 1, -1, -1):
        root = parents[index]
        if root != index:
            _merge_sources(glyphs[root], glyphs[index])
    return _retained_glyphs(glyphs, {index for index in range(len(glyphs)) if parents[index] != index})


def _retained_glyphs(glyphs: list[_Glyph], removed: set[int]) -> list[_Glyph]:
    """Clears out isolated whitespace where content has been removed on both sides, leaving spaces and line breaks between normal text."""
    if not removed:
        return glyphs
    trailing_removed = [True] * len(glyphs)
    following_removed = True
    for index in range(len(glyphs) - 1, -1, -1):
        trailing_removed[index] = following_removed
        if not glyphs[index].text.isspace():
            following_removed = index in removed
    result: list[_Glyph] = []
    previous_removed = True
    for index, glyph in enumerate(glyphs):
        orphaned = glyph.text.isspace() and previous_removed and trailing_removed[index]
        if not glyph.text.isspace():
            previous_removed = index in removed
        if index not in removed and not orphaned:
            result.append(glyph)
    return result


def _comparison_text(text: str) -> str:
    """Only the spacing and width forms are unified in copy comparisons, and the output still uses the visible original text."""
    return "".join(unicodedata.normalize("NFKC", text).split())


def _matches_hidden(hidden: str, visible: str) -> bool:
    """A small number of equal-length Chinese characters OCR are allowed to be replaced, and numbers, letters, additions and deletions must be strictly consistent."""
    a, b = _comparison_text(hidden), _comparison_text(visible)
    if not a or len(a) != len(b):
        return False
    if a == b:
        return True
    matches = sum(x == y for x, y in zip(a, b))
    return matches >= 6 and matches / len(a) >= 0.85 and all(x == y or (_is_han(x) and _is_han(y)) for x, y in zip(a, b))


def _union_boxes(boxes: list[Box]) -> Box:
    """Merge fragment ranges, only for candidate comparison, without changing the output geometry."""
    return min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)


def _visible_candidates(grid: dict[tuple[int, int], list[int]], box: Box, margin: float, cell: float) -> Iterator[int]:
    """Only access local grids; abnormal giant frames scan occupied grids instead to avoid traversing massive empty grids."""
    x0, x1 = math.floor((box[0] - margin) / cell), math.floor((box[2] + margin) / cell)
    y0, y1 = math.floor((box[1] - margin) / cell), math.floor((box[3] + margin) / cell)
    if (x1 - x0 + 1) * (y1 - y0 + 1) > 4 * len(grid):
        keys = ((x, y) for x, y in grid if x0 <= x <= x1 and y0 <= y <= y1)
    else:
        keys = ((x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1))
    for key in keys:
        yield from grid.get(key, ())


def _suppress_hidden(glyphs: list[_Glyph]) -> list[_Glyph]:
    """Filter hidden text geometries in Rust, Python keeping original Unicode match and copy rules."""
    native = get_native()
    if not any(g.head.get("text_render_mode") == 3 for g in glyphs):
        return glyphs
    if native is None:
        return _suppress_hidden_python(glyphs)
    objects, records = {}, []
    indices = [glyph.head.get("char_idx") for glyph in glyphs]
    if not all(isinstance(index, int) for index in indices):
        return _suppress_hidden_python(glyphs)
    ranks = {index: rank for rank, index in enumerate(sorted(set(indices)))}
    for glyph, index in zip(glyphs, indices):
        head = glyph.head
        origin = head.get("origin")
        if origin is not None and len(origin) != 2:
            return _suppress_hidden_python(glyphs)
        try:
            obj = objects.setdefault(head.get("text_object_id"), len(objects))
        except TypeError:
            return _suppress_hidden_python(glyphs)
        mode = head.get("text_render_mode")
        mode = int(mode) if mode in (0, 1, 2, 3, 4, 5, 6) else -1
        angle = glyph.angle if math.isfinite(glyph.angle) else 0.0
        records.append(
            (
                glyph.box,
                origin,
                obj,
                mode,
                bool(head.get("text_is_visible", True)),
                bool(glyph.text.strip()),
                angle,
                math.cos(angle),
                math.sin(angle),
                ranks[index],
            )
        )
    candidates = native.hidden_candidates(records)
    if candidates is None:
        return _suppress_hidden_python(glyphs)
    removed = set()
    for run, matches in candidates:
        if not _matches_hidden("".join(glyphs[i].text for i in run), "".join(glyphs[i].text for i in matches)):
            continue
        removed.update(run)
        if len(run) == len(matches) and all(len(glyphs[a].chars) == len(glyphs[b].chars) for a, b in zip(run, matches)):
            for a, b in zip(run, matches):
                _merge_sources(glyphs[b], glyphs[a])
        else:
            representative = glyphs[matches[0]]
            representative.chars = [c.copy() for c in representative.chars]
            representative.chars[0]["source_indices"] = _source_indices(
                representative.chars + [c for i in run for c in glyphs[i].chars]
            )
    return _retained_glyphs(glyphs, removed)


def _suppress_hidden_python(glyphs: list[_Glyph]) -> list[_Glyph]:
    """Suppress hiding OCR only if there is clearly visible original text at the same location, retaining the only text layer of the scanned page."""
    if not any(g.head.get("text_render_mode") == 3 for g in glyphs):
        return glyphs
    # Indexes visible character centers; cell size only affects speed and does not serve as text deletion threshold.
    cell = 32.0
    visible: dict[tuple[int, int], list[int]] = defaultdict(list)
    hidden_runs: list[list[int]] = []
    for i, g in enumerate(glyphs):
        if g.box is None or not g.text.strip():
            continue
        if g.head.get("text_render_mode") in (0, 1, 2, 4, 5, 6) and g.head.get("text_is_visible", True):
            b = g.box
            visible[(math.floor((b[0] + b[2]) / 2 / cell), math.floor((b[1] + b[3]) / 2 / cell))].append(i)
        elif g.head.get("text_render_mode") == 3:
            previous = glyphs[hidden_runs[-1][-1]] if hidden_runs else None
            same_run = (
                previous is not None
                and previous.head.get("text_object_id") == g.head.get("text_object_id")
                and abs(previous.angle - g.angle) <= _GEOMETRY_EPSILON
            )
            if same_run:
                pb, cb = _project(previous.box, g.angle), _project(g.box, g.angle)
                po, co = previous.head.get("origin"), g.head.get("origin")
                same_run = (
                    po is not None
                    and co is not None
                    and abs(-math.sin(g.angle) * (po[0] - co[0]) + math.cos(g.angle) * (po[1] - co[1])) <= _TRANSLATION_EPSILON
                    and cb[0] >= pb[0]
                    and cb[0] - pb[2] <= max(1.0, 0.6 * (cb[3] - cb[1]))
                )
            if not same_run:
                hidden_runs.append([])
            hidden_runs[-1].append(i)
    removed: set[int] = set()
    for run in hidden_runs:
        first = glyphs[run[0]]
        box = _union_boxes([glyphs[i].box for i in run])
        projected = _project(box, first.angle)
        margin = 0.1 * (projected[3] - projected[1])
        candidates: list[int] = []
        for i in _visible_candidates(visible, box, margin, cell):
            g = glyphs[i]
            if abs(g.angle - first.angle) > _GEOMETRY_EPSILON:
                continue
            p = _project(g.box, first.angle)
            if (
                projected[0] - margin <= (p[0] + p[2]) / 2 <= projected[2] + margin
                and projected[1] <= (p[1] + p[3]) / 2 <= projected[3]
            ):
                candidates.append(i)
        if not candidates:
            continue
        candidates.sort(key=lambda i: (_project(glyphs[i].box, first.angle)[0], glyphs[i].head["char_idx"]))
        vb = _union_boxes([_project(glyphs[i].box, first.angle) for i in candidates])
        axis_overlap = max(0.0, min(projected[2], vb[2]) - max(projected[0], vb[0]))
        normal_overlap = max(0.0, min(projected[3], vb[3]) - max(projected[1], vb[1]))
        if axis_overlap < 0.8 * (projected[2] - projected[0]) or normal_overlap < 0.8 * min(
            projected[3] - projected[1], vb[3] - vb[1]
        ):
            continue
        if _matches_hidden("".join(glyphs[i].text for i in run), "".join(glyphs[i].text for i in candidates)):
            removed.update(run)
            # One-to-one identical glyphs can accurately inherit their sources; the sources of typo fragments are uniformly attached to the representative characters.
            if len(run) == len(candidates) and all(
                len(glyphs[a].chars) == len(glyphs[b].chars) for a, b in zip(run, candidates)
            ):
                for a, b in zip(run, candidates):
                    _merge_sources(glyphs[b], glyphs[a])
            else:
                representative = glyphs[candidates[0]]
                representative.chars = [c.copy() for c in representative.chars]
                representative.chars[0]["source_indices"] = _source_indices(
                    representative.chars + [c for i in run for c in glyphs[i].chars]
                )
    return _retained_glyphs(glyphs, removed)


def deduplicate_chars(chars: list[Char]) -> list[Char]:
    """Unified entrance: protect glyph mapping, and then handle repeated drawings and hidden copies with visible correspondences."""
    if not chars:
        return []
    glyphs = _suppress_hidden(_collapse_paints(_mapping_groups(chars)))
    return [char for glyph in glyphs for char in glyph.chars]


__all__ = ["deduplicate_chars"]
