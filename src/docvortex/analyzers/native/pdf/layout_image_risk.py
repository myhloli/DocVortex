"""验证原生图框是否同时吞入局部表格和相邻的独立文字段落。"""

from bisect import bisect_left
from collections import defaultdict
import re

from .geometry import _bbox_overlap_in_first, _bbox_union_many, _coerce_bbox


def _projection_members(text):
    """按原生图内投影辨认真实交付给图像的字段行及连续文字，以便和源行精确配对。"""
    records, prose = set(), set()
    for line in text.splitlines():
        fields = re.split(r"[ \t]{2,}|\t", line.strip())
        key = "".join(line.split())
        if len(fields) >= 2:
            if any(c.isalpha() for c in fields[0]) and any(c.isdigit() for c in fields[-1]):
                records.add(key)
        elif sum(c.isalpha() for c in key) >= 16:
            prose.add(key)
    return records, prose


def _local_rule_bands(image, horizontal):
    """从大图内部取出只覆盖一侧、端点重复的横线，保留至少四个不同行带。"""
    left, top, right, bottom = image
    width = right - left
    tolerance = 0.005 * width
    groups = defaultdict(list)
    for x, y, end, base in horizontal:
        if (
            left - tolerance <= x < end <= right + tolerance
            and top <= y <= base <= bottom
            and 0.15 * width <= end - x <= 0.60 * width
            and max(x - left, right - end) >= 0.25 * width
        ):
            groups[round(x / tolerance), round(end / tolerance)].append((x, end, (y + base) / 2))
    for band in groups.values():
        levels = sorted({row[2] for row in band})
        if len(levels) >= 4:
            yield min(row[0] for row in band), max(row[1] for row in band), levels


def _prose_regions(boxes, horizontal):
    """按栏首、行高、行距连接连续说明行，横穿说明区的格线终止该段。"""
    columns = []
    for box in sorted(set(boxes)):
        if not columns or box[0] - columns[-1][0][0] > box[3] - box[1]:
            columns.append([])
        columns[-1].append(box)
    for column in columns:
        run = []
        for box in sorted(column, key=lambda b: b[1]):
            if run:
                prev = run[-1]
                h = prev[3] - prev[1]
                width = prev[2] - prev[0]
                lined = any(
                    prev[1] <= y <= box[1] and min(end, prev[2]) - max(x, prev[0]) >= 0.55 * width
                    for x, y, end, _ in horizontal
                )
                if lined or not (0.60 * h <= box[3] - box[1] <= 1.55 * h and 0.65 * h <= box[1] - prev[1] <= 1.9 * h):
                    if len(run) >= 3:
                        yield run
                    run = []
            run.append(box)
        if len(run) >= 3:
            yield run


def _table_regions(rows, bands):
    """把字段行与下边横线逐一配对，并按行距隔开同列但相距很远的两张表。"""
    for left, right, levels in bands:
        mapped = {}
        for box, key in rows:
            mid = (box[1] + box[3]) / 2
            i = bisect_left(levels, mid)
            if (
                left - 2 <= box[0]
                and box[2] <= right + 2
                and box[2] - box[0] >= 0.45 * (right - left)
                and i < len(levels)
                and levels[i] - mid <= 1.8 * (box[3] - box[1])
            ):
                mapped[i] = (box, key)
        run = []
        for i in sorted(mapped):
            box, key = mapped[i]
            if run and box[1] - run[-1][0][3] > 2.5 * (box[3] - box[1]):
                if len({k for _, k in run}) >= 4:
                    yield (left, run[0][0][1], right, run[-1][0][3]), len(run)
                run = []
            run.append((box, key))
        if len({k for _, k in run}) >= 4:
            yield (left, run[0][0][1], right, run[-1][0][3]), len(run)


def _independent_prose(runs, table, gutter):
    """表旁三行说明可独立成区；表上下须有同时并排的两个正文段，排除普通表注。"""
    left, top, right, bottom = table
    separate = []
    for run in runs:
        region = _bbox_union_many(run)
        if region[2] <= left - gutter or region[0] >= right + gutter:
            same_band = [b for b in run if top <= (b[1] + b[3]) / 2 <= bottom]
            if len(same_band) >= 3:
                return "旁侧连续正文", _bbox_union_many(same_band)
        if len(run) >= 4 and (region[3] < top or region[1] > bottom):
            line_height = run[0][3] - run[0][1]
            for old, other_height in separate:
                # 栏沟按段落自己的字号衡量；小字号的真实双栏可能只隔一个字高。
                column_gap = max(line_height, other_height)
                overlap = min(old[3], region[3]) - max(old[1], region[1])
                if (region[2] <= old[0] - column_gap or region[0] >= old[2] + column_gap) and overlap >= 0.55 * min(
                    old[3] - old[1], region[3] - region[1]
                ):
                    return "另外两栏连续正文", _bbox_union_many((old, region))
            separate.append((region, line_height))
    return None


def absorbed_table_and_prose(objects, size, horizontal, source_lines):
    """只对具有多列投影及单侧横线的候选读取源行，确认两个区域都未交付给独立原生块。"""
    if source_lines is None or len(horizontal) < 4:
        return None
    visible = None
    for kind, box, text, block in objects:
        if kind != "image" or block.get("angle") or box[2] - box[0] < 0.5 or box[3] - box[1] < 0.22 or "\n" not in text:
            continue
        records, prose = _projection_members(text)
        if len(records) < 4 or len(prose) < 3:
            continue
        image = tuple(v * s for v, s in zip(box, size * 2))
        bands = list(_local_rule_bands(image, horizontal))
        if not bands:
            continue
        if visible is None:
            visible = []
            owners = [
                tuple(v * s for v, s in zip(b, size * 2))
                for k, b, _, _ in objects
                if k in {"text", "paragraph_title", "doc_title", "table", "code", "algorithm", "caption", "header", "footer"}
            ]
            for line in source_lines():
                rect = _coerce_bbox(line["bbox"])
                if line["rotation"] or not rect or any(_bbox_overlap_in_first(rect, o) >= 0.8 for o in owners):
                    continue
                key = "".join("".join(s["text"] for s in line["spans"]).split())
                visible.append((rect, key))
        rows = [(b, key) for b, key in visible if _bbox_overlap_in_first(b, image) >= 0.95 and key in records]
        paragraphs = [b for b, key in visible if _bbox_overlap_in_first(b, image) >= 0.95 and key in prose]
        runs = list(_prose_regions(paragraphs, horizontal))
        for table, count in _table_regions(rows, bands):
            found = _independent_prose(runs, table, 0.04 * (image[2] - image[0]))
            if found:
                kind, region = found
                return (
                    f"大图独占 {count} 行带横线的字段表及{kind}；表区 {tuple(round(v) for v in table)} pt，"
                    f"正文区 {tuple(round(v) for v in region)} pt"
                )
    return None
