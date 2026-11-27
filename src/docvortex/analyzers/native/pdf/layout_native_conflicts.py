"""只度量块框的异常包含和表格跨越多列区域的长行吞并，类别偏差不扣分。"""

from statistics import median

from .geometry import _bbox_area, _bbox_overlap_in_first, _bbox_union, _rotate_bbox_to_upright


def _inside_text_corridor(child, line_boxes):
    """只把落入实际父行或稳定文字栏的子框计为侵入，排除父外框空白区的边注。"""
    if any(_bbox_overlap_in_first(child, line) >= 0.5 for line in line_boxes):
        return True
    left = sorted(box[0] for box in line_boxes)[int(0.1 * (len(line_boxes) - 1))]
    right = sorted(box[2] for box in line_boxes)[len(line_boxes) - 1 - int(0.1 * (len(line_boxes) - 1))]
    return left <= (child[0] + child[2]) / 2 <= right


def _physical_row_count(boxes):
    """相同物理基线上的字体片段只算一行，避免将单行导航栏误作多行正文。"""
    bands = []
    for box in sorted(boxes, key=lambda item: (item[1] + item[3]) / 2):
        center, height = (box[1] + box[3]) / 2, box[3] - box[1]
        if bands and center - bands[-1][0] <= 0.5 * max(height, bands[-1][1]):
            bands[-1] = (bands[-1][0], max(height, bands[-1][1]))
        else:
            bands.append((center, height))
    return len(bands)


def nested_block_risk(blocks):
    """同一个多行正文框内的独立小块按类型与面积占比累计扣分，对齐 0.80 默认回落门槛。"""
    best = None
    for parent in blocks:
        if parent.get("type") not in {"text", "list"} or len(parent.get("lines") or ()) < 3:
            continue
        outer = parent["bbox"]
        area = _bbox_area(outer)
        angle = int(parent.get("angle", 0) or 0)
        line_boxes = [
            _rotate_bbox_to_upright(tuple(line["bbox"]), (1.0, 1.0), angle) for line in parent["lines"] if line.get("bbox")
        ]
        if _physical_row_count(line_boxes) < 3:
            continue
        children = [
            other
            for other in blocks
            if other is not parent
            and other.get("type") in {"text", "list", "paragraph_title", "equation"}
            and 0 < _bbox_area(other["bbox"]) <= 0.4 * area
            and _bbox_overlap_in_first(other["bbox"], outer) >= 0.8
            and _inside_text_corridor(_rotate_bbox_to_upright(tuple(other["bbox"]), (1.0, 1.0), angle), line_boxes)
        ]
        if not children:
            continue
        points = min(
            60,
            sum(
                15 if other.get("type") == "equation" or _bbox_area(other["bbox"]) >= 0.10 * area else 8
                for other in children
            ),
        )
        if points and (best is None or points > best[0]):
            best = points, f"同一多行正文框异常包含 {len(children)} 个独立块，区域 {tuple(outer)}"
    return best


def _source_rows(lines, bounds, row_text=None, *, baseline=False):
    """按可见源字形的真实间隙重建物理行片段，保留真正分开的列间空白。"""
    parts = []
    for line in lines:
        for span in line.get("spans", ()):
            if span.get("rotation", 0) % 360:
                continue
            # PDF 的 span 可以跨多个单元格；以可见字形间隙确定列，不能把同一字体行当作正文。
            for char in span.get("chars", ()):
                if not str(char.get("char", "")).strip():
                    continue
                box = tuple(char.get("tight_bbox") or char["bbox"])
                if _bbox_overlap_in_first(box, bounds) >= 0.9 and box[3] > box[1]:
                    height = max(box[3] - box[1], char["bbox"][3] - char["bbox"][1])
                    origin = char.get("origin")
                    y = (origin[1] if origin is not None else char["bbox"][3]) if baseline else (box[1] + box[3]) / 2
                    parts.append((y, box, height, char["char"]))
    if not parts:
        return []
    em = median(height for _, _, height, _ in parts)
    band_gap = (0.4 if baseline else 0.5) * em
    bands = []
    for center, box, height, letter in sorted(parts):
        if not bands or center - bands[-1][0] > band_gap:
            bands.append((center, []))
        bands[-1][1].append((box, height, letter))
    rows = []
    for center, parts in bands:
        row_em = median(height for _, height, _ in parts)
        merged = []
        ordered = sorted(parts)
        if row_text is not None:
            row_text[center] = "".join(letter for _, _, letter in ordered)
        for box, _, _ in ordered:
            if merged and box[0] - merged[-1][2] <= row_em:
                merged[-1] = _bbox_union(merged[-1], box)
            else:
                merged.append(box)
        rows.append((center, merged, row_em))
    return rows


def _inside_closed_frame(bounds, geometry):
    """完整外边框是合并单元格的实物边界，框内长段落不作为跨表吞并。"""
    if geometry is None:
        return False
    left, top, right, bottom = bounds
    width, height = right - left, bottom - top
    tolerance = min(width, height) * 0.025
    vertical = [
        line.bbox
        for line in geometry.drawing_lines
        if line.orientation == "vertical" and min(line.bbox[3], bottom) - max(line.bbox[1], top) >= 0.85 * height
    ]
    horizontal = [
        line.bbox
        for line in geometry.drawing_lines
        if line.orientation == "horizontal" and min(line.bbox[2], right) - max(line.bbox[0], left) >= 0.85 * width
    ]
    return (
        any(abs((b[0] + b[2]) / 2 - left) <= tolerance for b in vertical)
        and any(abs((b[0] + b[2]) / 2 - right) <= tolerance for b in vertical)
        and any(abs((b[1] + b[3]) / 2 - top) <= tolerance for b in horizontal)
        and any(abs((b[1] + b[3]) / 2 - bottom) <= tolerance for b in horizontal)
    )


def _stable_column_gutters(rows):
    """按跨行重复的空白带确认列缝，单个宽单元格里的连续段落仍属于同一列。"""
    from collections import defaultdict

    events = defaultdict(list)
    for index, (_, parts, _) in enumerate(rows):
        for left, right in zip(parts, parts[1:]):
            if right[0] > left[2]:
                events[left[2]].append((index, 1))
                events[right[0]].append((index, -1))
    edges = sorted(events)
    active = defaultdict(int)
    required = max(2, (len(rows) + 1) // 2)
    gutters = []
    for left, right in zip(edges, edges[1:]):
        for index, change in events[left]:
            active[index] += change
            if not active[index]:
                del active[index]
        if len(active) >= required and right > left:
            if gutters and gutters[-1][1] == left:
                gutters[-1] = (gutters[-1][0], right)
            else:
                gutters.append((left, right))
    return gutters


def _authored_cell_texts(block):
    """仅相信来源明确且有多个非空单元格的 PDF 结构表；整页装进一格不能证明归属。"""
    if not block.get("_pdf_structure"):
        return ()
    markup = block.get("content")
    if not isinstance(markup, str) or not markup.startswith("<table"):
        return ()
    from bs4 import BeautifulSoup

    cells = ["".join(cell.get_text().split()) for cell in BeautifulSoup(markup, "html.parser").find_all(("td", "th"))]
    nonempty = [text for text in cells if text]
    return nonempty if len(nonempty) >= 2 else ()


def _right_column_continuation(line, flank, width, em):
    """长行与同侧两条带左栏标签的宽值列左缘对齐时，将续行保留在该列。"""
    return (
        sum(
            len(parts) >= 2
            and abs(parts[-1][0] - line[0]) <= em
            and parts[-1][2] - parts[-1][0] >= 0.35 * width
            and parts[-2][2] + em < line[0]
            for _, parts, _ in flank
        )
        >= 2
    )


def _attached_note_band(block, rows, lines, bounds, body_end):
    """表下小号抬高的注标与首行相接时，只保护其后同一连续行带内的表注。"""
    from .table_constants import _TABLE_CAPTION_RE

    content = block.get("content")
    if not isinstance(content, str) or not _TABLE_CAPTION_RE.match(content.partition("\n")[0].strip()):
        return None
    tail = [
        row
        for row in rows
        if row[0] > body_end and len(row[1]) == 1 and row[1][0][2] - row[1][0][0] >= 0.55 * (bounds[2] - bounds[0])
    ]
    if not tail:
        return None
    baseline, parts, em = tail[0]
    left = parts[0][0]
    for line in lines:
        spans = line.get("spans", ())
        chars = [char for span in spans for char in span.get("chars", ()) if char.get("char", "").strip()]
        if len(chars) != 1 or len(chars[0]["char"]) != 1 or not chars[0].get("origin"):
            continue
        char = chars[0]
        box = char.get("tight_bbox") or char["bbox"]
        if not (
            bounds[0] - em <= box[0] <= left + em
            and abs(box[2] - left) <= em
            and 0.2 * em <= baseline - char["origin"][1] <= em
            and box[3] - box[1] < 0.8 * em
        ):
            continue
        previous = baseline
        for y, segments, scale in rows:
            if y <= baseline:
                continue
            if len(segments) != 1 or y - previous > 1.8 * max(em, scale) or abs(segments[0][0] - left) > em:
                break
            previous = y
        return baseline, previous
    return None


def _outside_table_rows(rows, width):
    """定位跨过重复列缝、又远离多列行带的长行；至少三行才值得继续核实。"""
    cells = [row for row in rows if len(row[1]) >= 3 and row[1][-1][2] - row[1][0][0] >= 0.5 * width]
    if len(cells) < 2:
        return None
    gutters = _stable_column_gutters(cells)
    if len(gutters) < 2:
        return None
    top = min(box[1] for _, parts, _ in cells for box in parts)
    bottom = max(box[3] for _, parts, _ in cells for box in parts)
    outside = []
    for center, parts, em in rows:
        if len(parts) != 1 or parts[0][2] - parts[0][0] < 0.55 * width:
            continue
        if sum(parts[0][0] <= left and parts[0][2] >= right for left, right in gutters) < 2:
            continue
        if parts[0][3] < top - em or parts[0][1] > bottom + em:
            outside.append((center, parts[0], em))
    return (cells, top, bottom, outside) if len(outside) >= 3 else None


def _separate_framed_regions(geometry, bounds, rows):
    """两个实体描边面板之间仍有从框外起排的独立文字，证明外围表框跨越了阅读区域。"""
    if not rows:
        return False
    em = median(row[2] for row in rows)
    left, top, right, bottom = bounds
    frames = []
    paths = getattr(geometry, "path_infos", ())
    for path in paths:
        for box in path.rectangle_bboxes:
            x0, y0, x1, y1 = box
            if (
                left - em <= x0 < x1 <= right + em
                and top - em <= y0 < y1 <= bottom + em
                and x1 - x0 >= 0.6 * (right - left)
                and y1 - y0 >= 2 * em
                and any(
                    stroke.stroke_visible and max(abs(a - b) for a, b in zip(stroke.bbox, box)) < 0.4 * em for stroke in paths
                )
            ):
                frames.append(box)
    for a in frames:
        for b in frames:
            if b[1] - a[3] < 2 * em or abs(b[0] - a[0]) > em or abs(b[2] - a[2]) > em:
                continue
            if (
                sum(
                    a[3] < y < b[1] and parts[0][0] < min(a[0], b[0]) - em and parts[-1][2] > max(a[0], b[0]) + 3 * em
                    for y, parts, _ in rows
                )
                >= 2
            ):
                return True
    return False


def table_container_risk(blocks, size, lines, geometry=None):
    """多列行已经限定表体，而同一外框还吞下远离表体的连续长行时计分。"""
    best = None
    for block in blocks:
        if block.get("type") != "table" or block.get("angle") or _bbox_area(block["bbox"]) < 0.08:
            continue
        bounds = tuple(v * s for v, s in zip(block["bbox"], size * 2))
        width = bounds[2] - bounds[0]
        if _inside_closed_frame(bounds, geometry):
            continue
        # 墨迹行带先定位旧规则认为可疑的页，再用实际基线验证，避免校正行高时扩大回落范围。
        suspect = _outside_table_rows(_source_rows(lines, bounds), width)
        if suspect is None:
            continue
        row_text = {} if block.get("_pdf_structure") else None
        rows = _source_rows(lines, bounds, row_text, baseline=True)
        if _separate_framed_regions(geometry, bounds, rows):
            points = min(60, 10 * len(suspect[3]))
            if best is None or points > best[0]:
                best = points, f"同一表框跨过两个独立描边面板及其间的外部正文，区域 {tuple(block['bbox'])}"
            continue
        verified = _outside_table_rows(rows, width)
        if verified is None:
            continue
        cells, top, bottom, outside = verified
        authored = _authored_cell_texts(block)
        note_band = _attached_note_band(block, rows, lines, bounds, bottom)
        upper = [r for r in rows if r[0] < top]
        lower = [r for r in rows if r[0] > bottom]
        outside = [
            box
            for y, box, em in outside
            if not (note_band and note_band[0] <= y <= note_band[1])
            and not (authored and any(row_text[y] in cell for cell in authored))
            and not _right_column_continuation(box, upper if y < top else lower, width, em)
        ]
        points = min(60, 10 * len(outside)) if len(outside) >= 3 else 0
        if points and (best is None or points > best[0]):
            best = (
                points,
                f"同一表框在 {len(cells)} 条多列行限定区域之外还包含 {len(outside)} 条独立长行，区域 {tuple(block['bbox'])}",
            )
    return best
