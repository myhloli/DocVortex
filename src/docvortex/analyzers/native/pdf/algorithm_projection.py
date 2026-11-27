"""按源基线和真实行号投影已确认的编号伪代码。"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections import Counter, defaultdict
from statistics import median

from ....document.pdf.text._contracts import Char
from ....schema import BBox
from .spatial_text import _select_table_chars

_MARKER = re.compile(r"^( *)([0-9]{1,4})([.:]?)(?:\s+|$)(.*)")
_HEADING = re.compile(r"^\s*(?:algorithm|算法)\s*\d+\s*[:：.]", re.I)
_CONTROL = re.compile(r"(?:for|while|if|else|return|repeat|until)\b", re.I)
_SCRIPT = re.compile(r"[\w,]+")
_WORD_SEPARATORS = frozenset(",;:+-<>≤≥∈←→=−")


def numbered_algorithm_labels(content: str) -> list[str]:
    """在旧投影里确认算法题名及稳定左齐或右齐的连续行号槽。"""
    if not _HEADING.match(content):
        return []
    numbers, labels = [], []
    controls = 0
    for row in content.lstrip().splitlines()[1:]:
        marker = _MARKER.match(row)
        if marker is None:
            continue
        if not numbers:
            left, right = marker.span(2)
        if marker.start(2) == left or marker.end(2) == right:
            numbers.append(int(marker[2]))
            labels.append(marker[2] + marker[3])
            controls += bool(_CONTROL.match(marker[4]))
    return labels if len(numbers) >= 3 and controls >= 2 and all(b == a + 1 for a, b in zip(numbers, numbers[1:])) else []


def _source_row_markers(glyphs: list[Char], expected: list[str], em: float) -> list[tuple[float, list[Char]]]:
    """用最左侧窄行号槽锚定真实基线，拒绝没有同基线程序字形的编号。"""
    left = min(glyph["origin"][0] for glyph in glyphs)
    gutter: dict[float, list[Char]] = defaultdict(list)
    for glyph in glyphs:
        x, y = glyph["origin"]
        if left <= x <= left + 0.9 * em and glyph["char"] in "0123456789.:":
            gutter[round(y, 2)].append(glyph)
    rows = []
    for y, markers in sorted(gutter.items()):
        markers.sort(key=lambda glyph: glyph["origin"][0])
        if any(abs(glyph["origin"][1] - y) <= 0.05 * em and glyph["origin"][0] > markers[-1]["bbox"][2] for glyph in glyphs):
            rows.append((y, markers))
    if ["".join(glyph["char"] for glyph in markers) for _, markers in rows] != expected:
        return []
    return rows if all(0.9 * em <= b[0] - a[0] <= 2.5 * em for a, b in zip(rows, rows[1:])) else []


def _glyph_row_position(glyph: Char, em: float) -> tuple[float, bool] | None:
    """放大定界符按实际墨迹中心归行，超出单行尺寸或缺少清晰字形时交还旧投影。"""
    y = glyph["origin"][1]
    ink = glyph.get("tight_bbox")
    if glyph["char"] not in ("(", ")") or not ink or ink[3] - ink[1] < 1.05 * em:
        return y, False
    if ink[3] - ink[1] > 1.55 * em:
        return None
    return (ink[1] + ink[3]) / 2 + 0.24 * em, True


def _group_algorithm_rows(
    glyphs: list[Char], markers: list[tuple[float, list[Char]]], em: float
) -> tuple[float, list[list[tuple[Char, bool]]]] | None:
    """每个可见字形只归到唯一的行号基线或算法题名。"""
    marker_ids = {glyph["char_idx"] for _, row in markers for glyph in row}
    bases = [y for y, _ in markers]
    groups: list[list[tuple[Char, bool]]] = [[] for _ in range(len(bases) + 1)]
    for glyph in glyphs:
        if glyph["char_idx"] in marker_ids:
            continue
        position = _glyph_row_position(glyph, em)
        if position is None:
            return None
        y, tall = position
        if y < bases[0] - 0.7 * em:
            groups[0].append((glyph, tall))
            continue
        insertion = bisect_left(bases, y)
        # 严格递增的基线只可能在插入点左右各两条中取得最近和次近。
        neighbors = range(max(0, insertion - 2), min(len(bases), insertion + 2))
        nearest, alternate, *_ = sorted(neighbors, key=lambda n: abs(bases[n] - y))
        if abs(bases[nearest] - y) > 0.52 * em or abs(bases[alternate] - y) - abs(bases[nearest] - y) < 0.13 * em:
            return None
        groups[nearest + 1].append((glyph, tall))
    if any(not group for group in groups):
        return None
    heading_base = Counter(round(glyph["origin"][1], 2) for glyph, _ in groups[0]).most_common(1)[0][0]
    return heading_base, groups


def _project_algorithm_row(row: list[tuple[Char, bool]], baseline: float, em: float) -> tuple[str, float, bool] | None:
    """按主字形横坐标排列，仅把更小且紧邻字基的上下标附着一次。"""
    body: list[Char] = []
    scripts, tall_glyphs = [], []
    for glyph, tall in sorted(row, key=lambda entry: (entry[0]["origin"][0], entry[0]["char_idx"])):
        value = glyph["char"]
        offset = glyph["origin"][1] - baseline
        if any(character in value for character in ("'", '"', "`", "\ufffd")) or any(ord(char) < 32 for char in value):
            # 字符串内部字距不能仅凭比例字体的间隙重建。
            return None
        if tall:
            tall_glyphs.append(value)
        if tall or abs(offset) <= 0.09 * em:
            body.append(glyph)
            continue
        if not 0.09 * em < abs(offset) <= 0.47 * em or (glyph.get("font") or {}).get("size", em) > 0.84 * em:
            return None
        if offset < 0 and value == "′":
            kind = "prime"
        elif _SCRIPT.fullmatch(value):
            kind = "sub" if offset > 0 else "sup"
        else:
            return None
        scripts.append((glyph, kind))
    if not body or tall_glyphs not in ([], ["(", ")"]):
        return None
    attachments: dict[int, dict[str, list[Char]]] = defaultdict(lambda: defaultdict(list))
    parent = -1
    for glyph, kind in scripts:
        x = glyph["origin"][0]
        while parent + 1 < len(body) and body[parent + 1]["origin"][0] < x:
            parent += 1
        if parent < 0 or not body[parent]["char"].isalnum():
            return None
        attached = attachments[parent][kind]
        previous = attached[-1] if attached else body[parent]
        if not -0.24 * em <= x - previous["bbox"][2] <= 0.31 * em:
            return None
        attached.append(glyph)

    projected = []
    previous_edge, previous_char = 0.0, ""
    for i, base in enumerate(body):
        value = base["char"]
        threshold = 0.18 if value in _WORD_SEPARATORS or previous_char in _WORD_SEPARATORS else 0.23
        if (
            projected
            and base["origin"][0] - previous_edge >= threshold * em
            and value not in ",;:)]}"
            and previous_char not in "([{."
        ):
            projected.append(" ")
        projected.append(value)
        parts = attachments.get(i, {})
        projected.extend(glyph["char"] for glyph in parts.get("prime", ()))
        for kind, marker in (("sup", "^"), ("sub", "_")):
            if glyphs := parts.get(kind):
                projected.append(marker + "{" + "".join(glyph["char"] for glyph in glyphs) + "}")
        previous_edge = max([base["bbox"][2], *(glyph["bbox"][2] for items in parts.values() for glyph in items)])
        previous_char = value
    return "".join(projected), body[0]["origin"][0], bool(scripts or tall_glyphs)


def project_numbered_algorithm(chars: list[Char], bbox: BBox, angle: int, content: str) -> str:
    """只替换已确认编号伪代码的可信物理行，保留其它表格及二维复杂公式的原内容。"""
    if angle or not (labels := numbered_algorithm_labels(content)):
        return content
    glyphs = [glyph for glyph in _select_table_chars(chars, bbox) if glyph["char"].strip()]
    if not glyphs or any(
        glyph.get("origin") is None or glyph.get("is_generated") or glyph.get("char_idx") is None for glyph in glyphs
    ):
        return content
    if len({glyph["char_idx"] for glyph in glyphs}) != len(glyphs):
        return content
    em = Counter(round((glyph.get("font") or {}).get("size", 0.0), 2) for glyph in glyphs).most_common(1)[0][0]
    if em <= 0 or not (markers := _source_row_markers(glyphs, labels, em)):
        return content
    grouped = _group_algorithm_rows(glyphs, markers, em)
    if grouped is None:
        return content
    heading_base, groups = grouped
    rows = [
        _project_algorithm_row(group, baseline, em)
        for group, baseline in zip(groups, [heading_base, *(y for y, _ in markers)], strict=True)
    ]
    if any(row is None for row in rows):
        return content
    heading, *body = rows
    if len(content.strip().splitlines()) == len(rows) and not any(row[2] for row in rows):
        return content
    starts = sorted({round(row[1], 2) for row in body})
    gaps = [b - a for a, b in zip(starts, starts[1:]) if b - a > 0.7 * em]
    indent_pitch = median(gaps) if gaps else em
    levels = [(row[1] - starts[0]) / indent_pitch for row in body]
    if any(abs(level - round(level)) > 0.12 for level in levels):
        return content
    starts = [markers[0]["origin"][0] for _, markers in markers]
    ends = [markers[-1]["bbox"][2] for _, markers in markers]
    label_width = max(map(len, labels))
    padded_labels = (
        [label.ljust(label_width) for label in labels]
        if max(starts) - min(starts) < max(ends) - min(ends)
        else [label.rjust(label_width) for label in labels]
    )
    projected = "\n".join(
        [
            heading[0],
            *(
                label + " " + "  " * round(level) + row[0]
                for label, level, row in zip(padded_labels, levels, body, strict=True)
            ),
        ]
    )
    return projected if numbered_algorithm_labels(projected) == labels else content
