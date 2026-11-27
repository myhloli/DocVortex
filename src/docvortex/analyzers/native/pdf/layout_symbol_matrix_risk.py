"""从原件重复符号的行列轨道确认被横向裁片截断的矩阵图。"""

from collections import defaultdict
from itertools import pairwise
from statistics import median


def _strip_gaps(owners, width, height):
    """只检查上下两条宽而扁、列范围重叠的裁片之间的局部空带。"""
    strips = sorted(
        (box for box in owners if box[2] - box[0] >= 0.25 * width and 0 < box[3] - box[1] <= 0.12 * height),
        key=lambda box: box[1],
    )
    for i, upper in enumerate(strips):
        for j in range(i + 1, len(strips)):
            lower = strips[j]
            if lower[1] - upper[3] > 0.12 * height:
                break
            left, right = max(upper[0], lower[0]), min(upper[2], lower[2])
            if lower[1] > upper[3] and right - left >= 0.20 * width:
                yield upper, lower, left, right


def _tracks(values, tolerance):
    """将同一列或基线上微小的绘制坐标偏差合并，避免固定取整边界影响结果。"""
    tracks = {}
    anchor = None
    for value in sorted(set(values)):
        if anchor is None or value - anchor > tolerance:
            anchor = value
        tracks[value] = anchor
    return tracks


def _symbol_rows(paths, size):
    """按物理尺寸分组近方形填色符号，记录每条基线上的列位和实际填色。"""
    marks = []
    max_width = 0.05 * min(size)
    for path in paths:
        if not path.fill_visible or not path.fill_rgba or min(path.fill_rgba[:3]) >= 250:
            continue
        left, top, right, bottom = path.bbox
        w, h = right - left, bottom - top
        if 1 <= w <= max_width and 0.8 * w <= h <= 1.25 * w:
            marks.append((w, (left + right) / 2, (top + bottom) / 2, path.fill_rgba[:3]))
    groups = []
    for mark in sorted(marks):
        if not groups or mark[0] > 1.25 * groups[-1][0][0]:
            groups.append([])
        groups[-1].append(mark)
    for group in groups:
        if len(group) < 16:
            continue
        diameter = median(mark[0] for mark in group)
        xs = _tracks((mark[1] for mark in group), 0.2 * diameter)
        ys = _tracks((mark[2] for mark in group), 0.2 * diameter)
        rows = defaultdict(dict)
        for _, x, y, color in group:
            rows[ys[y]][xs[x]] = color
        yield diameter, sorted((y, cells) for y, cells in rows.items() if len(cells) >= 4)


def _matrix_in_gap(rows, diameter, gap, owners, width):
    """首尾裁片与中间裸露的符号须共用列轨、近等距行距，且矩阵内存在数据色差。"""
    upper, lower, left, right = gap
    local = []
    for y, cells in rows:
        if upper[1] <= y <= lower[3]:
            columns = {x for x in cells if left <= x <= right}
            if len(columns) >= 4:
                local.append((y, columns, cells))
    # 取贴近空带的两条已裁行作端点，避免一个框跨多行时把框内行也当成缺失。
    top = next((i for i in reversed(range(len(local))) if local[i][0] <= upper[3]), None)
    bottom = next((i for i, row in enumerate(local) if row[0] >= lower[1]), None)
    if top is None or bottom is None or bottom - top < 3:
        return None
    band = local[top : bottom + 1]
    steps = [b[0] - a[0] for a, b in pairwise(band)]
    if min(steps) < 0.8 * diameter or max(steps) > min(4 * diameter, 1.6 * min(steps)):
        return None
    shared = set.intersection(*(columns for _, columns, _ in band))
    if len(shared) < 4 or max(shared) - min(shared) < 0.20 * width:
        return None
    if len({cells[x] for _, _, cells in band for x in shared}) < 2:
        return None
    missing = 0
    for y, _, _ in band[1:-1]:
        intervals = [(a, c) for a, b, c, d in owners if b <= y <= d]
        if sum(not any(a <= x <= c for a, c in intervals) for x in shared) >= 4:
            missing += 1
            if missing >= 2:
                region = tuple(round(v, 1) for v in (min(shared), band[0][0], max(shared), band[-1][0]))
                return f"{len(shared)} 列填色符号在两个裁片间按行延续，至少两行各有四个符号未被承接，区域 {region} pt"
    return None


def symbol_matrix_conflict(objects, size, paths):
    """仅在完整图表无法解释跨裁片的有色点阵时报告结构冲突。"""
    if len(paths) < 16:
        return None
    width, height = size
    owners = [
        (box[0] * width, box[1] * height, box[2] * width, box[3] * height)
        for kind, box, _, _ in objects
        if kind in {"image", "chart", "table", "equation", "code", "algorithm"}
    ]
    # 无候选时不对整页路径分组；符号行在不同空带之间复用。
    rows = None
    for gap in _strip_gaps(owners, width, height):
        if rows is None:
            rows = list(_symbol_rows(paths, size))
            if not rows:
                return None
        for diameter, group in rows:
            if reason := _matrix_in_gap(group, diameter, gap, owners, width):
                return reason
    return None
