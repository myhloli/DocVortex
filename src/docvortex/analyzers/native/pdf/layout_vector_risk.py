"""用实际绘线和路径确认严重缺图或截表，几何坐标统一为 PDF point。"""

from bisect import bisect_right
from collections import defaultdict
import re

from ....content.spans import inline_span_plain_text
from .annotation_text import _is_strong_caption_text
from .geometry import (
    _bbox_area,
    _bbox_overlap_in_first,
    _bbox_union,
    _coerce_bbox,
    _rotate_bbox_from_upright,
    _rotate_bbox_to_upright,
)
from .layout_diagnostics import _union_coverage
from .layout_image_risk import absorbed_table_and_prose
from .layout_symbol_matrix_risk import symbol_matrix_conflict
from .layout_table_risk import _AMOUNT, _continued_numeric_rows


def _page_objects(blocks):
    """读取最终块；长正文、已交付的图表和有效图注可解释原页的笔画。"""
    for block in blocks:
        box = _coerce_bbox(block.get("bbox"))
        if not box:
            continue
        content = block.get("content")
        text = inline_span_plain_text(content) if isinstance(content, list) else content or ""
        yield block["type"], box, text, block


def wants_vectors(blocks):
    """仅复核图面有明显空档、文本碎块密集或表体截断的页面。"""
    objects = list(_page_objects(blocks))
    small = sum(kind in {"text", "paragraph_title"} and _bbox_area(b) < 0.010 for kind, b, _, _ in objects)
    covered = sum(_bbox_area(b) for kind, b, _, _ in objects if kind not in {"header", "footer", "page_number"})
    return (
        covered < 0.46
        or small >= 9
        or any(kind == "image" and "\n" in text and re.search(r"\S {2,}\S", text) for kind, _, text, _ in objects)
    )


def _table_edges(objects, width, height, horizontal, vertical, paths):
    """验证跨过已有表底的长纵轨、延续至图外的行底色，或完全缺少容器的数字网格。"""
    blocks = [b for _, _, _, b in objects]
    covers = [rect for typ, rect, _, _ in objects if typ in {"table", "image", "chart"}]
    for typ, box, text, _ in objects:
        if typ not in {"table", "image"}:
            continue
        left, top, right, bottom = (v * scale for v, scale in zip(box, (width, height, width, height)))
        if typ == "table" and right - left > width * 0.25:
            tracks = sorted(
                {
                    round(x)
                    for x, y, _, v in vertical
                    if left - 2 <= x <= right + 2 and y < bottom - 6 and v > bottom + 0.08 * height
                }
            )
            if (
                len(tracks) >= 3
                and tracks[-1] - tracks[0] > 0.55 * (right - left)
                and _continued_numeric_rows(box, blocks) >= 3
            ):
                return f"至少三条纵轨跨过表底，表外仍有多行同列金额，原生表框 {box}"
        if typ == "image" and "\n" in text and re.search(r"\S {2,}\S", text):
            # 只用纵向反复出现、横向轨道相同的有色矩形。须同时覆盖框内及多条框外记录。
            groups = defaultdict(list)
            for path in paths:
                x, y, z, v = path.bbox
                if (
                    path.rectangle_bboxes
                    and path.fill_visible
                    and path.fill_rgba
                    and max(path.fill_rgba[:3]) - min(path.fill_rgba[:3]) > 12
                    and left - 2 <= x < z <= right + 2
                    and z - x > (right - left) * 0.23
                    and v - y >= 4
                ):
                    groups[round(x / 2), round(z / 2), path.fill_rgba].append((y, v))
            for bands in groups.values():
                if not any(top <= y < bottom for y, _ in bands):
                    continue
                outside = sorted({(y, v) for y, v in bands if bottom - 2 <= y < bottom + 0.47 * height})
                if len(outside) < 3 or outside[0][0] - bottom > 0.12 * height:
                    continue
                populated = {
                    y
                    for y, v in outside
                    if any(
                        k == "text" and box[0] <= br[0] < box[2] and y - 3 <= br[1] * height <= v + 2 for k, br, _, _ in objects
                    )
                }
                if len(populated) >= 3 and max(populated) - min(populated) > 0.08 * height:
                    return f"表格图只覆盖前部，框外仍有 {len(populated)} 行同轨底纹及独立记录，区域 {box}"
    # 完全没有表框的情形：相同列轨被连续横线切过，每条行带内必须有游离数值。
    numbers = [
        (b[0] * width, (b[1] + b[3]) * height / 2)
        for k, b, s, _ in objects
        if k == "text"
        and len(s) < 15
        and re.fullmatch(r"[+-]?\d[\d,.%]*", s.strip())
        and not any(_bbox_overlap_in_first(b, c) > 0.7 for c in covers)
    ]
    # 开放式小表也可能只有一道表头横线和一条跨行纵线，靠两行独立数值块复核实际列轨。
    if len(numbers) >= 6:
        for x, y, _, bottom in vertical:
            if bottom - y < height * 0.028:
                continue
            for left, top, right, _ in horizontal:
                if not (left + 4 < x < right - 4 and y < top < bottom and right - left > width * 0.22):
                    continue
                bands = defaultdict(set)
                for nx, ny in numbers:
                    if left <= nx <= right and top < ny <= bottom:
                        bands[round(ny / 2)].add(round(nx / 3))
                supported = [(row, cols) for row, cols in bands.items() if len(cols) >= 3]
                if any(
                    2 <= b - a <= height * 0.04 and sum(any(abs(v - u) <= 1 for u in cols2) for v in cols1) >= 3
                    for a, cols1 in supported
                    for b, cols2 in supported
                ):
                    return f"横线分隔表头，纵轨两侧的表区没有容器，至少两行三列数值各自成块，区域 {(round(left), round(y), round(right), round(bottom))} pt"
    if len(numbers) >= 9:
        for x, y, _, bottom in vertical:
            if bottom - y < height * 0.12:
                continue
            bars = [
                (a, t, r)
                for a, t, r, _ in horizontal
                if a + 2 < x < r - 2 and y - 2 <= t <= bottom + 2 and r - a > width * 0.22
            ]
            if len({round(t) for _, t, _ in bars}) < 6:
                continue
            left, right = min(a for a, _, _ in bars), max(r for _, _, r in bars)
            values = [(a, t) for a, t in numbers if left <= a <= right and y <= t <= bottom]
            if (
                len(values) >= 9
                and len({round(t / 3) for _, t in values}) >= 5
                and max(a for a, _ in values) - min(a for a, _ in values) > (right - left) * 0.3
            ):
                return f"源页网格内多行数值落在独立文本中，区域 {(round(left), round(y), round(right), round(bottom))} pt"
    return None


def _rows_below_header(cells, upper, lower, barriers):
    """要求表头及多条物理数据行各有独立文字框，并重复落在同一组列内。"""
    left, top, right = upper
    band = lower - top
    local = [
        (b, text)
        for b, text in cells
        if left - 1 <= b[0] < b[2] <= right + 1 and b[1] >= top - 1 and b[2] - b[0] < 0.45 * (right - left)
    ]
    heading = sorted(((b, t) for b, t in local if b[3] <= lower + 1 and b[1] < lower - 1), key=lambda field: field[0][1])
    if len(heading) < 4:
        return None
    first = heading[0][0]
    header_row = [(box, text) for box, text in heading if abs(box[1] - first[1]) <= 0.45 * (first[3] - first[1])]
    # 排除单纯按格陈列字母或符号的字形样张；至少两列有可辨认的文字列名。
    if sum(sum(ch.isalpha() for ch in text) >= 2 for _, text in header_row) < 2:
        return None
    fields = sorted(box for box, _ in header_row)
    if (
        len(fields) < 4
        or fields[-1][2] - fields[0][0] < 0.65 * (right - left)
        or any(a[2] >= b[0] for a, b in zip(fields, fields[1:]))
    ):
        return None
    separators = [(a[2] + b[0]) / 2 for a, b in zip(fields, fields[1:])]
    stop = min(
        (b[1] for b in barriers if b[1] > lower and min(right, b[2]) - max(left, b[0]) > 0.55 * (right - left)),
        default=float("inf"),
    )
    rows = []
    for box, _ in sorted(local, key=lambda pair: pair[0][1]):
        x, y, z, bottom = box
        if y < lower - 1:
            continue
        if y >= stop:
            break
        if (not rows and y - lower > band) or (rows and y - rows[-1][1] > 3 * band):
            break
        column = bisect_right(separators, (x + z) / 2)
        if (column and x < separators[column - 1] - 1) or (column < len(separators) and z > separators[column] + 1):
            continue
        if not rows or y - rows[-1][0] > min(4.0, 0.3 * band):
            rows.append([y, bottom, {column}])
        else:
            rows[-1][1] = max(rows[-1][1], bottom)
            rows[-1][2].add(column)
    repeated = [r for r in rows if len(r[2]) >= 4]
    for i, (start, _, tracks) in enumerate(repeated):
        common = tracks
        matched = 1
        for _, end, columns in repeated[i + 1 :]:
            shared = common & columns
            if len(shared) < 4:
                continue
            common = shared
            matched += 1
            if matched >= 3 and end - start >= 2 * band:
                return (left, top, right, end), len(common), matched
    return None


def _fragmented_ruled_table(objects, size, drawing_lines):
    """按块的阅读方向统一表头线和文字坐标，仅检查没有图表容器承接的表格区域。"""
    if len(drawing_lines) < 2 or len(objects) < 16:
        return None
    containers = [b for kind, b, _, _ in objects if kind in {"table", "image", "chart", "code", "algorithm"}]
    by_angle = defaultdict(list)
    for kind, box, text, block in objects:
        if kind not in {"text", "paragraph_title", "doc_title"} or not text.strip():
            continue
        angle = block.get("angle", 0) or 0
        if angle in {0, 90, 180, 270}:
            by_angle[angle].append((box, text))
    for angle, items in by_angle.items():
        if len(items) < 16:
            continue
        width, height = size[::-1] if angle in (90, 270) else size
        direction = "vertical" if angle in (90, 270) else "horizontal"
        rails = []
        for line in drawing_lines:
            if line.orientation != direction:
                continue
            left, top, right, bottom = _rotate_bbox_to_upright(line.bbox, size, angle)
            if right - left >= 0.25 * width:
                rails.append((left, (top + bottom) / 2, right))
        if len(rails) < 2:
            continue

        def upright(box):
            """将归一化输出框转换为本方向的阅读坐标（PDF point）。"""
            return _rotate_bbox_to_upright(tuple(v * s for v, s in zip(box, size * 2)), size, angle)

        cells = [(upright(b), text) for b, text in items if not any(_bbox_overlap_in_first(b, c) >= 0.75 for c in containers)]
        if len(cells) < 16:
            continue
        barriers = [upright(b) for b in containers] + [b for b, _ in cells]
        rails.sort(key=lambda r: r[1])
        for i, upper in enumerate(rails):
            for j in range(i + 1, len(rails)):
                other = rails[j]
                if other[1] - upper[1] > 0.09 * height:
                    break
                if other[1] - upper[1] < 4 or max(abs(other[0] - upper[0]), abs(other[2] - upper[2])) > 0.012 * width:
                    continue
                if found := _rows_below_header(cells, upper, other[1], barriers):
                    region, cols, rows = found
                    physical = tuple(round(v, 1) for v in _rotate_bbox_from_upright(region, size, angle))
                    return f"表头两条平行边线之间有独立列名，其下 {rows} 行共享的 {cols} 个文字列反复拆散，阅读方向 {angle}°，区域 {physical} pt"
    return None


def _repeated_detached_numbers(objects, left, right, boundary_y, rails, *, upward):
    """沿朝向框外的纵轴，在横线带内查找至少四行共用三个数值列的独立碎块。"""
    width = right - left
    stop = rails[-1]
    cells = []
    ink = []
    for kind, rect, text, block in objects:
        x, y, end, base = rect
        if upward:
            y, base = -base, -y
        if base <= boundary_y or y >= stop or end <= left or x >= right:
            continue
        # 已交付的邻表、题注及独立正文截断搜索，即使题注很短或被误标为文本。
        if kind in {"table", "image", "chart", "caption", "code", "algorithm"} or (
            kind in {"text", "paragraph_title", "doc_title"} and (end - x > 0.55 * width or _is_strong_caption_text(text))
        ):
            stop = min(stop, y)
            continue
        center = (y + base) / 2
        if left <= (x + end) / 2 <= right and text.strip():
            ink.append(center)
        if (
            kind in {"text", "paragraph_title"}
            and not block.get("angle")
            and left <= x < end <= right
            and end - x <= 0.20 * width
            and 0 < base - y <= 0.035
            and _AMOUNT.fullmatch(text.strip())
        ):
            cells.append((center, base - y, end))
    if len(cells) < 12:
        return None
    # 两道同宽线之间若有明显留白且无内容，可能已换表；不跨空带借用数字。
    for above, below in zip([boundary_y, *rails], rails):
        if below - above > 0.026 and not any(above <= y <= below for y in ink):
            stop = min(stop, above)
            break
    rows = []
    for center, height, right_edge in sorted(cells):
        if center >= stop:
            break
        if not rows or center - rows[-1][0] > 0.4 * min(height, rows[-1][1]):
            rows.append((center, height, []))
        rows[-1][2].append(right_edge)
    rows = [(y, sorted(edges)) for y, _, edges in rows if len(edges) >= 3]
    for i, (first, edges) in enumerate(rows):
        if first - boundary_y > 0.10:
            break
        tracks = []
        for right_edge in edges:
            # 每个种子列至少间隔两倍的对齐容差，同一文字块不能证明两条轨道。
            if not tracks or right_edge - tracks[-1] > 0.024 * width:
                tracks.append(right_edge)
        matched = 1
        for y, edges in rows[i + 1 :]:
            shared = [x for x in tracks if any(abs(x - e) <= 0.012 * width for e in edges)]
            if len(shared) < 3 or shared[-1] - shared[0] < 0.30 * width:
                continue
            matched += 1
            tracks = shared
            if matched >= 4 and bisect_right(rails, y) > bisect_right(rails, first):
                return matched, len(tracks), y
    return None


def _partial_ruled_table(objects, size, drawing_lines):
    """从已有表框的上下边沿同宽源横线向外查找，两个方向共用碎列和屏障判断。"""
    tables = [b for k, b, _, block in objects if k == "table" and not block.get("angle") and b[2] - b[0] >= 0.25]
    if not tables or len(objects) < 13 or len(drawing_lines) < 4:
        return None
    width, height = size
    horizontal = [
        (line.bbox[0] / width, (line.bbox[1] + line.bbox[3]) / (2 * height), line.bbox[2] / width)
        for line in drawing_lines
        if line.orientation == "horizontal"
    ]
    for box in tables:
        left, top, right, bottom = box
        tol = 0.008 * (right - left)
        rails = sorted({y for x, y, end in horizontal if max(abs(x - left), abs(end - right)) <= tol})
        if len(rails) < 4:
            continue
        for upward in (False, True):
            # 检查表顶时把纵坐标取反，两种情况下都从框边朝坐标增大的方向搜索。
            inner, edge = (-bottom, -top) if upward else (top, bottom)
            levels = [-y for y in reversed(rails)] if upward else rails
            if not any(inner - 0.003 <= y < edge - 0.008 for y in levels) or not any(abs(y - edge) <= 0.003 for y in levels):
                continue
            outside = []
            for y in levels[bisect_right(levels, edge + 0.003) :]:
                if y - (outside[-1] if outside else edge) > 0.08:
                    break
                outside.append(y)
            if len(outside) >= 2 and (match := _repeated_detached_numbers(objects, left, right, edge, outside, upward=upward)):
                rows, cols, last = match
                side, physical_y = ("表顶", -last) if upward else ("表底", last)
                return f"同宽横线延续至{side}外，{rows} 行各有 {cols} 个同列数值被拆成独立块，原生表框 {box}，延至 y={physical_y:.3f}"
    return None


def vector_conflict(blocks, size, geometry, *, source_lines=None):
    """核对绘制区域与输出载体，只有真实图形或表格证据才产生接管理由。"""
    width, height = size
    paths = [p for p in geometry.path_infos if p.fill_visible or p.stroke_visible]
    objects = list(_page_objects(blocks))
    if reason := _fragmented_ruled_table(objects, size, geometry.drawing_lines):
        return "table_structure", reason
    if reason := _partial_ruled_table(objects, size, geometry.drawing_lines):
        return "table_structure", reason
    horizontal = [line.bbox for line in geometry.drawing_lines if line.orientation == "horizontal"]
    if reason := absorbed_table_and_prose(objects, size, horizontal, source_lines):
        return "absorbed_table_and_prose", reason
    if len(paths) < 3:
        return None
    vertical = [line.bbox for line in geometry.drawing_lines if line.orientation == "vertical"]
    if reason := _table_edges(objects, width, height, horizontal, vertical, paths):
        return "table_structure", reason
    if reason := symbol_matrix_conflict(objects, size, paths):
        return "fragmented_symbol_matrix", reason
    covers = [
        (b[0] * width, b[1] * height, b[2] * width, b[3] * height)
        for kind, b, text, _ in objects
        if kind in {"image", "chart", "table", "code", "algorithm", "equation"} or (kind == "text" and len(text) > 90)
    ]
    area = width * height
    frames = set()
    nodes = set()
    traces = []
    dots = []
    for path in paths:
        b = path.bbox
        x, y, z, v = b
        w, h = z - x, v - y
        if w <= 0 or h <= 0:
            continue
        if (
            (path.rectangle_bboxes or path.segment_count <= 6)
            and 0.02 * area <= w * h <= 0.78 * area
            and w > 0.15 * width
            and h > 0.07 * height
        ):
            frames.add(b)
        if (
            (path.stroke_visible or path.fill_visible)
            and 4 <= path.segment_count <= 28
            and 0.045 * width < w < 0.65 * width
            and 0.016 * height < h < 0.29 * height
            and 1.1 < w / h < 18
        ):
            nodes.add(b)
        if path.stroke_visible and path.segment_count >= 8 and w > 0.10 * width and h > 0.021 * height:
            traces.append(b)
        if (
            path.fill_visible
            and path.fill_rgba
            and max(path.fill_rgba[:3]) - min(path.fill_rgba[:3]) > 18
            and path.segment_count >= 10
            and 2 <= w <= 0.04 * width
            and 0.7 <= w / h <= 1.45
        ):
            dots.append(b)
    for rect in sorted(frames, key=_bbox_area, reverse=True)[:64]:
        x, y, z, v = rect
        w, h = z - x, v - y
        if _union_coverage(rect, covers) >= 0.45 * w * h:
            continue
        axes = any(a <= x + 0.18 * w and c >= z - 0.18 * w and y - 2 <= b <= v + 2 for a, b, c, _ in horizontal) and any(
            b <= y + 0.18 * h and d >= v - 0.18 * h and x - 2 <= a <= z + 2 for a, b, _, d in vertical
        )
        if not axes:
            continue
        curves = [
            b for b in traces if _bbox_overlap_in_first(b, rect) > 0.9 and b[2] - b[0] > 0.40 * w and b[3] - b[1] > 0.10 * h
        ]
        if len(set(curves)) >= 2:
            return "missing_chart", f"有坐标轴及多条独立走势的图面缺少对应容器，区域 {rect} pt"
        points = {(round((a + c) / 2), round((b + d) / 2)) for a, b, c, d in dots if x <= a < c <= z and y <= b < d <= v}
        if (
            len(points) >= 7
            and len({p[0] for p in points}) >= 5
            and len({p[1] for p in points}) >= 5
            and max(a for a, _ in points) - min(a for a, _ in points) > 0.35 * w
            and max(b for _, b in points) - min(b for _, b in points) > 0.22 * h
        ):
            return "missing_chart", f"带轴区域中的 {len(points)} 个散点没有图面承接，区域 {rect} pt"
    # 两条跨留白的连线至少将三个独立节点连成图。一个被框住的面板或一排装饰底色均不成立。
    if 3 <= len(nodes) <= 300:
        nodes = [n for n in sorted(nodes) if _union_coverage(n, covers) < 0.5 * _bbox_area(n)]
        neighbours = defaultdict(set)
        tips = defaultdict(list)
        for path in paths:
            a, t, c, d = path.bbox
            if path.fill_visible and path.segment_count in {3, 4} and 0.6 <= c - a <= 7 and 0.6 <= d - t <= 7:
                for column in range(int(a), int(c) + 1):
                    tips[column].append((t, d))

        for x, y, z, v in vertical:
            if v - y < height * 0.009 or v - y > height * 0.30:
                continue
            mid = (x + z) / 2
            # 有独立实心箭头时，线段止于箭杆；用实际三角形的尖端核对节点边界。
            upper = min([y, *(top for top, bottom in tips.get(int(mid), ()) if abs(bottom - y) < 1)])
            lower = max([v, *(bottom for top, bottom in tips.get(int(mid), ()) if abs(top - v) < 1)])
            above = [n for n in nodes if n[0] + 1 < mid < n[2] - 1 and abs(n[3] - upper) < 2]
            below = [n for n in nodes if n[0] + 1 < mid < n[2] - 1 and abs(n[1] - lower) < 2]
            for a in above:
                for b in below:
                    neighbours[a].add(b)
                    neighbours[b].add(a)
        for center, adjacent in neighbours.items():
            if len({(round((n[0] + n[2]) / 8), round((n[1] + n[3]) / 8)) for n in adjacent}) < 2:
                continue
            region = center
            for n in adjacent:
                region = _bbox_union(region, n)
            if _bbox_area(region) > 0.038 * area:
                return "missing_diagram", f"多个带框节点有跨空白的连线，图面未被承接，区域 {region} pt"
    return None
