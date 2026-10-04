"""Only fill a space based on the reliable ink margins of adjacent glyphs, without overwriting the source characters."""

from __future__ import annotations

import math
import unicodedata
from functools import lru_cache


@lru_cache(maxsize=2048)
def _ordinary_non_cjk(text: str) -> bool:
    """Only a single ordinary letter or decimal number is accepted, excluding Chinese, Japanese and Korean characters and compatible glyphs."""
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
    """Only the coordinate axis is rotated without translation; the adjacent gap does not depend on the page size."""
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
    """Narrow fonts in monospaced fonts have naturally large white spaces, and no word boundaries are added when advance evidence is lacking."""
    name = name.lower()
    return not flags & 1 and not any(token in name for token in ("mono", "courier", "consolas", "menlo", "typewriter", "fixed"))


def needs_tight_space(left, right, *, tight_bboxes=None, origins=None) -> bool:
    """Determines whether adjacent non-CJK characters in the same direction have an ink gap of more than one-quarter font size."""
    if not _ordinary_non_cjk(str(left.get("char", ""))) or not _ordinary_non_cjk(str(right.get("char", ""))):
        return False
    # Narrow glyphs in consecutive numbers (such as 11) will also produce large ink margins, conservatively leaving the numbers as they are.
    if left["char"].isdecimal() and right["char"].isdecimal():
        return False
    li, ri = left.get("char_idx"), right.get("char_idx")
    # Do not cross filtered characters, explicit whitespace, other span or container-claimed character complement boundaries.
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
        # Significantly different font sizes are usually superscript and subscript. If there is insufficient evidence, no additional spaces will be added.
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
        # Some PDF write the font size as 1 and then use the text matrix to enlarge it; there is no forced comparison between the source font size and the page ink.
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
    """Rebuild short lines in order of existing members, inserting only single spaces at adjacent reliable word boundaries."""
    parts = []
    previous = None
    for char in chars:
        if previous is not None and needs_tight_space(previous, char, tight_bboxes=tight_bboxes, origins=origins):
            parts.append(" ")
        parts.append(str(char.get("char", "")))
        previous = char
    return "".join(parts)
