"""Restore separate legends and legends with symbols by consecutive physical lines next to the figure, without relying on text in the figure."""

from __future__ import annotations

import statistics
import re

from .geometry import _bbox_axis_overlap_ratio, _bbox_center_y
from .text_assembly.common import _merge_internal_text_block_group
from .text_assembly.continuity import _reassemble_member_lines
from .visual_annotations import _is_strong_caption_text, _is_strong_footnote_text


def _annotation_em(block):
    """Determine legend adjacency dimensions based on the original row height to avoid fixing page coordinates or pixel thresholds."""
    return statistics.median(block.get("_line_heights") or [block["bbox"][3] - block["bbox"][1]])


def recover_image_annotation_bands(blocks, page_size, drawing_lines):
    """Restore captions and legends with repeated symbols starting only from the explicit numbering immediately adjacent to the picture."""
    removed = set()
    for image in [block for block in blocks if block.get("type") == "image"]:
        bounds = image["bbox"]
        seeds = [
            block
            for block in blocks
            if _is_strong_caption_text(str(block.get("content", "")))
            and block.get("_text_lines")
            and block["bbox"][0] < bounds[2]
            and re.fullmatch(
                r"\s*(?:fig(?:ure)?\.?|diagram|图)\s*(?:\d+[A-Za-z]?|[IVX]+)[.:：]?\s*", str(block["content"]), re.I
            )
            and (
                abs(block["bbox"][3] - bounds[1]) < 1.5 * _annotation_em(block)
                or 0 <= block["bbox"][1] - bounds[3] < 4 * _annotation_em(block)
            )
        ]
        if not seeds:
            continue
        seed = min(seeds, key=lambda b: min(abs(b["bbox"][3] - bounds[1]), abs(b["bbox"][1] - bounds[3])))
        heights = seed.get("_line_heights") or [seed["bbox"][3] - seed["bbox"][1]]
        em = statistics.median(heights)
        leading = seed["bbox"][3] <= bounds[1] + em
        seed["type"] = "caption"
        seed["_annotation_band_parent"] = image
        candidates = [
            b
            for b in blocks
            if b is not image
            and id(b) not in removed
            and b.get("_text_lines")
            and b.get("type") in {"text", "paragraph_title", "caption", "footnote"}
            and b["bbox"][1] >= bounds[3] - 0.25 * em
            and _bbox_axis_overlap_ratio(b["bbox"], bounds, axis="x") >= 0.5
        ]
        candidates.sort(key=lambda b: (b["bbox"][1], b["bbox"][0]))
        if not candidates:
            continue
        first = candidates[0] if leading else seed
        if first["bbox"][1] - bounds[3] > 4 * em:
            continue
        left, right = first["bbox"][0], max(first["bbox"][2], bounds[2])
        if not leading:
            # The first row of fragments is numbered very narrowly, and subsequent actual full rows give the legend width.
            followers = [b for b in candidates if b["bbox"][1] <= first["bbox"][3] + em]
            right = max([right] + [b["bbox"][2] for b in followers])
        group = []
        bottom = first["bbox"][1]
        for block in candidates:
            b = block["bbox"]
            if b[0] < left - em or b[2] > right + em:
                continue
            if b[1] - bottom > 1.4 * em:
                break
            if leading and b[2] - b[0] < 0.65 * (right - left):
                break
            if block is not seed and _is_strong_caption_text(str(block.get("content", ""))):
                break
            if _is_strong_footnote_text(str(block.get("content", ""))):
                break
            group.append(block)
            bottom = max(bottom, b[3])
        if not group:
            continue
        merged = _merge_internal_text_block_group(group, list(range(len(group))))
        merged["content"] = _reassemble_member_lines(merged["_text_lines"], merged["content"])
        merged["type"] = "caption"
        merged["_annotation_band_parent"] = image
        removed.update(id(b) for b in group)
        blocks.append(merged)
        if not leading:
            continue
        separators = [
            rule.bbox[1]
            for rule in drawing_lines
            if rule.orientation == "horizontal"
            and rule.bbox[1] > bottom
            and rule.bbox[2] - rule.bbox[0] >= 0.8 * (right - left)
            and abs(rule.bbox[0] - left) < 2 * em
        ]
        if not separators:
            continue
        end = min(separators)
        legend = [
            b
            for b in blocks
            if id(b) not in removed
            and b.get("_text_lines")
            and b.get("type") in {"text", "paragraph_title"}
            and bottom <= b["bbox"][1] < end
            and left - em <= b["bbox"][0] < right
        ]
        symbols = [b for b in legend if len(str(b.get("content", "")).strip()) == 1 and str(b["content"]).strip().isalpha()]
        if len(symbols) < 3:
            continue
        # Multi-column legends are grouped by the actual left edge of the name, with smaller letters in the row claiming only the nearest left edge of the name.
        physical_rows = []
        for block in sorted(legend, key=lambda b: (_bbox_center_y(b["bbox"]), b["bbox"][0])):
            row = next(
                (
                    r
                    for r in reversed(physical_rows)
                    if abs(_bbox_center_y(r[0]["bbox"]) - _bbox_center_y(block["bbox"])) < 0.65 * em
                ),
                None,
            )
            if row is None:
                physical_rows.append([block])
            else:
                row.append(block)
        rows = []
        for physical in physical_rows:
            group = []
            for block in sorted(physical, key=lambda b: b["bbox"][0]):
                if group and block["bbox"][0] - group[-1]["bbox"][2] > 3 * em:
                    rows.append(group)
                    group = []
                group.append(block)
            if group:
                rows.append(group)
        heading_tops = [b["bbox"][1] for b in legend if b.get("type") == "paragraph_title"]
        starts = sorted(row[0]["bbox"][0] for row in rows if len(str(row[0].get("content", ""))) > 2)
        columns = []
        for start in starts:
            if columns and start - statistics.median(columns[-1]) <= 1.5 * em:
                columns[-1].append(start)
            else:
                columns.append([start])
        stable_lefts = [statistics.median(column) for column in columns if len(column) >= 3]
        for row in rows:
            note = _merge_internal_text_block_group(row, list(range(len(row))), preserve_visual_spaces=True)
            note["type"] = "footnote"
            note["_annotation_band_parent"] = image
            if len(stable_lefts) >= 2 and note["bbox"][2] - note["bbox"][0] < 0.65 * (right - left):
                # The classification title opens a new group; the columns in the group are from top to bottom to avoid inserting the subcategory title in the right column into the drug name in the left column.
                section = max([bottom] + [top for top in heading_tops if top <= note["bbox"][1] + 0.25 * em])
                lane = min(stable_lefts, key=lambda start: abs(start - note["bbox"][0]))
                if any(block.get("type") == "paragraph_title" for block in row):
                    lane = left - em
                note["_legend_sort_band"] = (section, lane)
            blocks.append(note)
            removed.update(id(b) for b in row)
    blocks[:] = [block for block in blocks if id(block) not in removed]
