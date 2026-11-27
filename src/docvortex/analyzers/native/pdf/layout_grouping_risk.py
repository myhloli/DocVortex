"""从交付块中核对仍然丢失的项目、程序和表格边界，用于区分高分页。"""

from __future__ import annotations

import re
from itertools import pairwise
from statistics import median

from .annotation_text import _is_strong_caption_text
from .geometry import _bbox_center_y, _bbox_overlap_in_first
from .layout_risk import _block_text
from .table_constants import _TABLE_CAPTION_RE

_NUMBER = re.compile(r"[+−–—-]?\d+(?:[.,]\d+)*(?:[%*∗])?|[−–—-]")


def _collapsed_paragraph_points(blocks, read_lines):
    """一个段落吞入多个源项目时，逐项核对行首文字与左缘；单个高矩形不足以扣分。"""
    if read_lines is None:
        return 0
    candidates = []
    for block in blocks:
        if block.get("type") != "text" or not block.get("_pdf_structure"):
            continue
        box = block["bbox"]
        if box[3] - box[1] < 0.1 or len(block.get("lines", ())) != 1:
            continue
        text = _block_text(block)
        # 段内已交付显式换行时，项目仍可分别阅读。
        spans = block["content"] if isinstance(block["content"], list) else []
        if (
            text.count("•") >= 2
            and "\n" not in text
            and not any(span.get("type") in {"line_break", "hard_break"} for span in spans if isinstance(span, dict))
        ):
            # 大段若还吞掉印在单独物理行的加粗编号标题，已有列表和章节两种层级损失。
            headings = (
                {
                    "".join(span["content"].split())
                    for span in spans
                    if isinstance(span, dict)
                    and "bold" in (span.get("styles") or ())
                    and isinstance(span.get("content"), str)
                    and re.match(r"^\s*\d+(?:\.\d+)+\s+\S", span["content"])
                }
                if box[3] - box[1] >= 0.5
                else set()
            )
            candidates.append(("".join(text.split()), headings))
    if not candidates:
        return 0

    starts = []
    physical_text = set()
    for row in read_lines():
        text = "".join(span.get("text", "") for span in row.get("spans", ())).strip()
        physical_text.add("".join(text.split()))
        if text.startswith("•") and sum(c.isalpha() for c in text) >= 4 and (box := row.get("bbox")) is not None:
            starts.append((box, "".join(text.split())[:20]))
    best = 0
    for text, headings in candidates:
        matching = [box for box, anchor in starts if anchor in text]
        for box in matching:
            em = box[3] - box[1]
            # 同一行内印刷的并列符号不算项目间的段界。
            others = {b[1] for b in matching if abs(b[0] - box[0]) <= 0.5 * em and b[1] >= box[1] + 0.75 * em}
            if others and headings & physical_text:
                return 30
            best = max(best, 3 * len(others))
    return best


def _lost_code_breaks(blocks):
    """程序体被提升成标题、闭括号却作为同一缩进的独立正文输出，是一处可验证的归组冲突。"""
    headings = [
        b
        for b in blocks
        if b.get("type") in {"paragraph_title", "doc_title"}
        and not b.get("angle")
        and re.search(r"\{[^{}]*;\s*$", _block_text(b))
    ]
    if not headings:
        return 0
    closers = [
        b["bbox"] for b in blocks if b.get("type") == "text" and not b.get("angle") and _block_text(b).strip() in {"}", "};"}
    ]
    count = 0
    for block in headings:
        head = block["bbox"]
        for box in closers:
            em = box[3] - box[1]
            if abs(box[0] - head[0]) <= 1.1 * em and -0.3 * em <= box[1] - head[3] <= 1.6 * em:
                # 标题内各行及随后孤立的闭括号都缺少代码归属。
                count += max(1, len(block.get("lines") or ())) + 1
                break
    return count


def _rows_at_caption(rows, edge, direction, em):
    """从表题向表体逐行查找稳定的文字与数值列；留白、错轨或段落终止连续证据。"""
    tracks = None
    count = 0
    for center, row in rows:
        distance = direction * (center - edge)
        if distance < 0:
            continue
        if distance > (2.3 if count else 3) * em:
            break
        numeric = sum(is_value for _, is_value in row)
        if len(row) < 4 or numeric < 2 or numeric == len(row):
            if not count and len(row) >= 3:
                continue
            break
        ordered = sorted(row)
        if any(first[0][2] > second[0][0] + 0.1 * em for first, second in pairwise(ordered)):
            break
        edges = [(b[0] + b[2]) / 2 if is_value else b[0] for b, is_value in ordered]
        tracks = edges if tracks is None else [x for x in tracks if any(abs(x - y) <= em for y in edges)]
        if len(tracks) < 3:
            break
        edge = center
        count += 1
    return count if count >= 3 else 0


def _lost_table_rows(blocks):
    """表题旁的独立短文本须连续组成多行同轨的混合数据，正确表体内部及无题散列不计。"""
    if len(blocks) < 13:
        return 0
    captions = [
        b["bbox"]
        for b in blocks
        if b.get("type") in {"text", "caption", "table_caption", "paragraph_title"}
        and not b.get("angle")
        and _TABLE_CAPTION_RE.match(text := _block_text(b))
        and _is_strong_caption_text(text)
    ]
    if not captions:
        return 0
    containers = [b["bbox"] for b in blocks if b.get("type") in {"image", "table", "chart", "code"}]
    cells = []
    for b in blocks:
        if b.get("type") not in {"text", "paragraph_title"} or b.get("angle") or len(b.get("lines", ())) > 1:
            continue
        box, text = b["bbox"], _block_text(b).strip()
        if 0 < len(text) <= 38 and not any(_bbox_overlap_in_first(box, outer) >= 0.5 for outer in containers):
            cells.append((box, bool(_NUMBER.fullmatch(text))))
    best = 0
    for left, top, right, bottom in captions:
        subset = [
            (box, val)
            for box, val in cells
            if left - 0.008 <= box[0] and box[2] <= right + 0.008 and box[2] - box[0] <= 0.45 * (right - left)
        ]
        if len(subset) < 12:
            continue
        em = median(box[3] - box[1] for box, _ in subset)
        rows = []
        for cell in sorted(subset, key=lambda item: _bbox_center_y(item[0])):
            center = _bbox_center_y(cell[0])
            if not rows or center - rows[-1][0] > 0.45 * em:
                rows.append((center, []))
            rows[-1][1].append(cell)
        best = max(best, _rows_at_caption(rows, bottom, 1, em), _rows_at_caption(reversed(rows), top, -1, em))
    return best


def grouping_loss_points(blocks, read_lines=None):
    """沿用每处三个百分点的碎裂尺度，按最重一类残缺计分，完整归组后证据自行消失。"""
    # 每一行已证实至少四个独立单元，至少需要恢复三处水平边界。
    return min(
        30, max(3 * _lost_code_breaks(blocks), 9 * _lost_table_rows(blocks), _collapsed_paragraph_points(blocks, read_lines))
    )


def displaced_note_points(blocks):
    """同一源段在同一印刷行续写，远处的脚注却作为正文插在对应上标之后，每处轻扣三个百分点。"""
    points = 0
    for first, note, rest in zip(blocks, blocks[1:], blocks[2:]):
        owner = (first.get("_pdf_structure") or {}).get("node_id")
        if not owner or (rest.get("_pdf_structure") or {}).get("node_id") != owner:
            continue
        note_owner = (note.get("_pdf_structure") or {}).get("node_id")
        if not note_owner or note_owner == owner or any(b.get("type") != "text" or b.get("angle") for b in (first, note, rest)):
            continue
        a, n, c = first["bbox"], note["bbox"], rest["bbox"]
        if (
            min(a[3], c[3]) <= max(a[1], c[1])
            or n[1] <= max(a[3], c[3]) + 0.025
            or min(a[2], c[2], n[2]) <= max(a[0], c[0], n[0])
        ):
            continue
        spans = first.get("content")
        if not isinstance(spans, list) or not spans:
            continue
        marker = spans[-1]
        if not isinstance(marker, dict) or "superscript" not in (marker.get("styles") or ()):
            continue
        num = str(marker.get("content") or "").strip()
        match = re.match(r"([0-9]{1,3})\s+", _block_text(note).lstrip())
        if match and match[1] == num:
            points += 3
    return min(9, points)
