"""用局部行框和独立块的成员关系识别连续段落、程序或整式的碎裂。"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
import re

from ....content.spans import inline_span_plain_text
from .geometry import _bbox_axis_overlap_ratio, _bbox_overlap_in_first, _bbox_union, _coerce_bbox
from .layout_program_risk import _STATEMENT

_LIST_START = re.compile(r"^(?:[•▪◦◆—]|\(?[0-9一二三四五六七八九十]+[.)）、]|\[\d+\]|第.{1,5}条)")
_WORD = re.compile(r"[^\W\d_]+")
_DISPLAY = re.compile(r"[∑∫∂√=±×÷∇]|\b(?:cos|sin|tan|log)\b")


def _continued_runs(rows, *, program):
    """在同一栏内连接物理行距与栏缘相符的独立块，终止于句尾、项目符或明显留白。"""
    rows.sort(key=lambda item: item[0][1])
    tops = [box[1] for box, _ in rows]
    lengths = [1] * len(rows)
    origins = [box for box, _ in rows]
    for i, (box, text) in enumerate(rows):
        height = box[3] - box[1]
        if not program and text.endswith((".", "。", "！", "？", "?", "!", ":", "：", "；", ";")):
            continue
        stop = bisect_right(tops, box[3] + 0.85 * height, lo=i + 1)
        for j in range(i + 1, stop):
            following, content = rows[j]
            if following[1] < box[3] - 0.30 * height or abs(following[0] - box[0]) > (0.026 if program else 0.012):
                continue
            if not 0.65 * height <= following[3] - following[1] <= 1.55 * height:
                continue
            if not program and (abs(following[2] - box[2]) > 0.028 or content[:1].isupper()):
                continue
            if lengths[i] + 1 > lengths[j]:
                lengths[j] = lengths[i] + 1
                origins[j] = origins[i]
            if lengths[j] >= (4 if program else 5):
                return lengths[j], _bbox_union(origins[j], following)
    return None


def _interlocking_equations(boxes):
    """同一基线的至少三个相连裁片存在重叠成员时，确认整式被重复裁切与横向拆断。"""
    items = sorted(boxes, key=lambda r: r[1])
    tops = [r[1] for r in items]
    for i, anchor in enumerate(items):
        stop = bisect_left(tops, anchor[3], lo=i + 1)
        for other in items[i + 1 : stop]:
            small, large = sorted((anchor, other), key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))
            if _bbox_overlap_in_first(small, large) < 0.55:
                continue
            union = _bbox_union(anchor, other)
            # 独立的竖排公式与上下对齐公式没有这条横向相连的第三片。
            for piece in items:
                if piece == anchor or piece == other or _bbox_axis_overlap_ratio(piece, union, axis="y") < 0.55:
                    continue
                if max(union[0] - piece[2], piece[0] - union[2]) > min(0.018, 0.65 * (union[3] - union[1])):
                    continue
                if min(piece[0], union[0]) < union[0] - 0.012 or max(piece[2], union[2]) > union[2] + 0.012:
                    return _bbox_union(union, piece)
    return None


def _detached_entries(blocks):
    """当一片长正文吞并多个独立条目、左侧标签仍逐行悬空时，要求两侧印刷基线对齐。"""
    labels = []
    bodies = []
    for b in blocks:
        if b.get("angle") or not (box := _coerce_bbox(b.get("bbox"))):
            continue
        if b.get("type") == "text" and box[3] - box[1] > 0.22 and len(b.get("lines", ())) >= 10:
            bodies.append((box, [r["bbox"] for r in b["lines"] if r.get("bbox")]))
        if b.get("type") not in {"text", "header", "paragraph_title"} or box[2] - box[0] > 0.18:
            continue
        value = b.get("content")
        text = inline_span_plain_text(value) if isinstance(value, list) else value
        if isinstance(text, str) and re.fullmatch(r"\[[\w.:/-]{1,20}\]", text.strip()):
            labels.append(box)
    for box, lines in bodies:
        aligned = [
            label
            for label in labels
            if 0 <= box[0] - label[2] < 0.05
            and box[1] - 0.005 <= label[1] <= box[3]
            and any(abs(label[1] - line[1]) < 0.006 for line in lines)
        ]
        if len(aligned) >= 5 and max(b[1] for b in aligned) - min(b[1] for b in aligned) > 0.15:
            return box
    return None


def fragmented_region(blocks):
    """限定在同一可定位区域，验出整段分行、程序语句分散以及公式的横向或二维碎片。"""
    prose, program, math = [], [], []
    gaps, comments, denominators = [], [], []
    equations = [box for b in blocks if b.get("type") == "equation" and (box := _coerce_bbox(b.get("bbox")))]
    if len(equations) >= 3 and (region := _interlocking_equations(equations)):
        return "formula_ownership", f"同一展示式的重叠裁片还与第三片横向相接，区域 {region}"
    if len(blocks) >= 6 and (entries := _detached_entries(blocks)):
        return "fragmented_text", f"多个条目标识独立而对应各项正文被合成一个跨行长框，区域 {entries}"
    if len(equations) >= 4:
        wide = [b for b in equations if b[2] - b[0] > 0.74 and min(b[0], 1 - b[2]) < 0.035]
        for ref in wide:
            strips = [b for b in wide if abs(ref[0] - b[0]) < 0.012 and abs(ref[2] - b[2]) < 0.012]
            if len(strips) < 4:
                continue
            damaged = set()
            for block in blocks:
                if block.get("type") != "text":
                    continue
                for line in block.get("lines") or ():
                    if not (rect := _coerce_bbox(line.get("bbox"))) or rect[2] - rect[0] < 0.35:
                        continue
                    damaged.update(eq for eq in strips if _bbox_overlap_in_first(rect, eq) > 0.55)
            if len(damaged) >= 2:
                return (
                    "formula_ownership",
                    f"同一页边重复的宽公式裁片横穿多条正文，区域 {_bbox_union(min(damaged), max(damaged))}",
                )
    for b in blocks:
        if b.get("type") not in {"text", "paragraph_title", "doc_title"} or b.get("angle"):
            continue
        box = _coerce_bbox(b.get("bbox"))
        if not box:
            continue
        content = b.get("content")
        text = (inline_span_plain_text(content) if isinstance(content, list) else content or "").strip()
        lines = b.get("lines") or ()
        if len(equations) >= 3 and len(lines) >= 3 and _DISPLAY.search(text):
            inside = [eq for eq in equations if _bbox_overlap_in_first(eq, box) >= 0.80]
            for eq in inside:
                peers = [other for other in inside if _bbox_axis_overlap_ratio(eq, other, axis="y") >= 0.65]
                peers.sort()
                if len(peers) >= 3 and all(left[2] <= right[0] for left, right in zip(peers, peers[1:])):
                    return (
                        "formula_ownership",
                        f"同条展示式横向分成 {len(peers)} 个裁片，另有带运算符的多行文本穿过，区域 {box}",
                    )
        if b["type"] == "text" and len(lines) == 2 and len(text) >= 30:
            first, last = (_coerce_bbox(line.get("bbox")) for line in lines)
            if first and last and _bbox_axis_overlap_ratio(first, last, axis="x") > 0.80:
                height = first[3] - first[1]
                if 0.65 * height < last[1] - first[3] < 1.6 * height:
                    gaps.append((box, first[3], last[1]))
        if len(lines) != 1 or not 0.003 < box[3] - box[1] < 0.065:
            continue
        if text.startswith("//"):
            comments.append(box)
        if len(text) <= 180 and text.endswith((";", "{", "}")) and _STATEMENT.search(text):
            program.append((box, text))
        if b["type"] == "text" and len(text) >= 15 and not _LIST_START.match(text) and box[2] - box[0] >= 0.095:
            if len(_WORD.findall(text)) >= 4 or sum("\u3400" <= c <= "\u9fff" for c in text) >= 10:
                prose.append((box, text))
        term = re.fullmatch(r"\d*([A-Za-zα-ωΑ-Ω])\d+", text)
        if term and isinstance(content, list):
            styled = any(style in {"subscript", "superscript"} for span in content for style in span.get("styles", ()))
            math.append((box, term[1], styled))
        elif text.isdecimal() and len(text) <= 4:
            denominators.append(box)
    # 两段同栏正文均发生跨行认领，要求缺口内确有另一个从属的单行块。
    split = []
    for box, top, bottom in gaps:
        if any(
            abs(line[0] - box[0]) < 0.012
            and _bbox_axis_overlap_ratio(box, line, axis="x") > 0.8
            and top <= (line[1] + line[3]) / 2 <= bottom
            for line, _ in prose
        ):
            if any(abs(old[0] - box[0]) < 0.012 and abs(old[1] - box[1]) < 0.18 for old in split):
                return "fragmented_text", f"同栏多段的中间行被独立框切出，段框反复跨过缺失行，区域 {box}"
            split.append(box)
    if len(comments) >= 3:
        pairs = [
            box
            for box, _ in program
            if any(box[2] <= c[0] <= box[2] + 0.28 and _bbox_axis_overlap_ratio(box, c, axis="y") >= 0.7 for c in comments)
        ]
        for i, box in enumerate(pairs):
            if sum(abs(box[0] - other[0]) <= 0.02 and abs(box[1] - other[1]) < 0.12 for other in pairs[i:]) >= 3:
                return "fragmented_text", f"相邻程序行的语句和同行注释反复落入独立块，区域 {box}"
    for rows, is_program in ((program, True), (prose, False)):
        if len(rows) >= 4 and (result := _continued_runs(rows, program=is_program)):
            count, box = result
            unit = "程序语句" if is_program else "连续正文"
            return "fragmented_text", f"同栏{unit}逐行落入 {count} 个独立块，区域 {box}"
    # 同一分子至少三个同变量项、实测上下标，加上其下独立居中的分母，排除普通多列数值行。
    if len(math) >= 3 and denominators:
        for base, variable, styled in math:
            if not styled:
                continue
            peers = sorted(
                box for box, symbol, _ in math if symbol == variable and _bbox_axis_overlap_ratio(box, base, axis="y") > 0.65
            )
            if len(peers) < 3 or any(not 0 <= b[0] - a[2] <= 0.06 for a, b in zip(peers, peers[1:])):
                continue
            row = _bbox_union(peers[0], peers[-1])
            mid = (row[0] + row[2]) / 2
            if any(
                0 <= box[1] - row[3] <= 0.8 * (row[3] - row[1]) and abs((box[0] + box[2]) / 2 - mid) < 0.13 * (row[2] - row[0])
                for box in denominators
            ):
                return "formula_ownership", f"分子的 {len(peers)} 个同变量项和居中分母各自散成文本，区域 {row}"
    return None
