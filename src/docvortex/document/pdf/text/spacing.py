"""只根据相邻字形的可靠墨迹留白补一个空格，不改写源字符。"""

from __future__ import annotations

import math
import unicodedata
from functools import lru_cache


@lru_cache(maxsize=2048)
def _ordinary_non_cjk(text: str) -> bool:
    """仅接受单个普通字母或十进制数字，排除中日韩文字及兼容字形。"""
    if len(text) != 1:
        return False
    code = ord(text)
    if (
        0x1100 <= code <= 0x11FF
        or 0x2E80 <= code <= 0xA4CF
        or 0xA960 <= code <= 0xA97F
        or 0xAC00 <= code <= 0xD7FF
        or 0xF900 <= code <= 0xFAFF
        or 0xFF66 <= code <= 0xFF9F
        or 0x1AFF0 <= code <= 0x1B16F
        or 0x20000 <= code <= 0x323AF
    ):
        return False
    category = unicodedata.category(text)
    return category.startswith("L") or category == "Nd"


def _local_box(box, angle):
    """只旋转坐标轴而不平移；相邻间隙不依赖页面尺寸。"""
    x0, y0, x1, y1 = box
    if angle == 90:
        return y0, -x1, y1, -x0
    if angle == 180:
        return -x1, -y1, -x0, -y0
    if angle == 270:
        return -y1, x0, -y0, x1
    return x0, y0, x1, y1


@lru_cache(maxsize=512)
def _font_allows_spacing(name: str, flags: int) -> bool:
    """等宽字体的窄字形有天然大留白，缺少 advance 证据时不新增词界。"""
    name = name.lower()
    return not flags & 1 and not any(token in name for token in ("mono", "courier", "consolas", "menlo", "typewriter", "fixed"))


def needs_tight_space(left, right, *, tight_bboxes=None, origins=None) -> bool:
    """判定同向相邻非 CJK 字符是否具有超过四分之一字号的墨迹间隙。"""
    if not _ordinary_non_cjk(str(left.get("char", ""))) or not _ordinary_non_cjk(str(right.get("char", ""))):
        return False
    # 连续数字中的窄字形（如 11）也会产生大墨迹留白，保守保持数字原样。
    if left["char"].isdecimal() and right["char"].isdecimal():
        return False
    li, ri = left.get("char_idx"), right.get("char_idx")
    # 不跨被过滤字符、显式空白、其他 span 或容器认领的字符补词界。
    if not isinstance(li, int) or not isinstance(ri, int) or ri != li + 1:
        return False
    try:
        for char in (left, right):
            if not _font_allows_spacing(str(char["font"].get("name", "")), int(char["font"].get("flags", 0))):
                return False
        ls, rs = float(left["font"]["size"]), float(right["font"]["size"])
        if not math.isfinite(ls) or not math.isfinite(rs) or min(ls, rs) <= 0:
            return False
        em = max(ls, rs)
        # 字号显著不同通常是上下标，证据不足时不额外补空格。
        if min(ls, rs) < 0.8 * em:
            return False
        la, ra = float(left["writing_angle"]), float(right["writing_angle"])
        if not math.isfinite(la) or not math.isfinite(ra):
            return False
        degrees = math.degrees(la) % 360
        angle = int(round(degrees / 90) * 90) % 360
        if abs((degrees - angle + 180) % 360 - 180) > 0.1:
            return False
        if abs((math.degrees(ra) - angle + 180) % 360 - 180) > 0.1:
            return False
        lb = (tight_bboxes or {}).get(li, left.get("tight_bbox"))
        rb = (tight_bboxes or {}).get(ri, right.get("tight_bbox"))
        if lb is None or rb is None or len(lb) != 4 or len(rb) != 4:
            return False
        if not all(math.isfinite(v) for v in (*lb, *rb)):
            return False
        a, b = _local_box(lb, angle), _local_box(rb, angle)
        if a[2] <= a[0] or b[2] <= b[0] or a[3] <= a[1] or b[3] <= b[1]:
            return False
        # 有些 PDF 把字号写成 1 再用文字矩阵放大；不拿源字号和页面墨迹强行比较。
        if a[3] - a[1] > 1.5 * ls or b[3] - b[1] > 1.5 * rs:
            return False
        if b[0] - a[2] <= 0.25 * em:
            return False
        overlap = min(a[3], b[3]) - max(a[1], b[1])
        if overlap < 0.5 * min(a[3] - a[1], b[3] - b[1]):
            return False
        lo = (origins or {}).get(li, left.get("origin"))
        ro = (origins or {}).get(ri, right.get("origin"))
        if lo is not None and ro is not None:
            if not all(math.isfinite(v) for v in (*lo, *ro)):
                return False
            perpendicular = 0 if angle in (90, 270) else 1
            if abs(lo[perpendicular] - ro[perpendicular]) > 0.15 * em:
                return False
        elif abs(a[3] - b[3]) > 0.15 * em:
            return False
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return True


def join_tight_text(chars, *, tight_bboxes=None, origins=None) -> str:
    """按现有成员顺序重建短行，仅在相邻可靠词界插入单个空格。"""
    parts = []
    previous = None
    for char in chars:
        if previous is not None and needs_tight_space(previous, char, tight_bboxes=tight_bboxes, origins=origins):
            parts.append(" ")
        parts.append(str(char.get("char", "")))
        previous = char
    return "".join(parts)
