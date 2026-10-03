"""仅在字重缺失时，以同页同字符的原生字形像素补充粗体证据。"""

from collections import defaultdict
from dataclasses import replace
import math
import statistics

from PIL import Image

from .detection import _char_font_styles, _coerce_bbox, _pdf_font_metadata, detect_pdf_text_style_lines


def _glyph_mask(image, bbox, scale):
    """归一化单字符墨迹，排除过小或无墨迹区域并保持相同字符比较。"""
    crop = image.crop(tuple(math.floor(v * scale) if i < 2 else math.ceil(v * scale) for i, v in enumerate(bbox)))
    try:
        if crop.width < 6 or crop.height < 9:
            return None
        mask = crop.point(lambda value: 255 if value < 128 else 0)
        try:
            normalized = mask.resize((32, 32), Image.Resampling.NEAREST)
            try:
                pixels = normalized.get_flattened_data() if hasattr(normalized, "get_flattened_data") else normalized.getdata()
                ink = frozenset(index for index, value in enumerate(pixels) if value)
            finally:
                normalized.close()
        finally:
            mask.close()
        if not 0.08 <= len(ink) / 1024 <= 0.85:
            return None
        return ink, crop.width / crop.height
    finally:
        crop.close()


def glyph_weight_style_lines(lines, geometry, render):
    """缺失字重且有足量对照字符时才渲染；证据不足保持原结果，兼容只提供快照的来源。"""
    groups = defaultdict(lambda: defaultdict(list))
    counts = defaultdict(int)
    for line in lines:
        if line.angle != 0:
            continue
        for char in line.chars:
            text = str(char.get("char", ""))
            if len(text) != 1 or not text.isascii() or not text.isalpha():
                continue
            font = _pdf_font_metadata(char)
            if font[1] & (1 << 6) or "bold" in _char_font_styles(char):
                continue
            bbox = geometry.tight_bboxes.get(char.get("char_idx")) or _coerce_bbox(char.get("tight_bbox"))
            if bbox is None:
                continue
            counts[font] += 1
            if len(groups[font][text]) < 3:
                groups[font][text].append(bbox)
    eligible = [font for font in groups if counts[font] >= 20 and len(groups[font]) >= 8]
    if render is None or len(eligible) < 2:
        return []
    reference = max(eligible, key=lambda font: counts[font])
    candidates = [
        font
        for font in eligible
        if font != reference and font[2] is None and len(groups[font].keys() & groups[reference].keys()) >= 8
    ]
    if not candidates:
        return []
    rendered = render()
    image = rendered.pil_image.convert("L")
    try:
        masks = {}
        for font in [reference, *candidates]:
            masks[font] = {}
            for text, boxes in groups[font].items():
                samples = [value for box in boxes if (value := _glyph_mask(image, box, rendered.scale)) is not None]
                if samples:
                    masks[font][text] = sorted(samples, key=lambda value: len(value[0]))[len(samples) // 2]
        bold_fonts = set()
        for font in candidates:
            ratios = []
            for text in masks[font].keys() & masks[reference].keys():
                target, target_aspect = masks[font][text]
                normal, normal_aspect = masks[reference][text]
                if not 0.75 <= target_aspect / normal_aspect <= 1.33 or len(target & normal) / len(target | normal) < 0.4:
                    continue
                ratios.append(len(target) / len(normal))
            if (
                len(ratios) >= 8
                and statistics.median(ratios) >= 1.4
                and sum(ratio >= 1.3 for ratio in ratios) / len(ratios) >= 0.75
            ):
                bold_fonts.add(font)
    finally:
        image.close()
        rendered.pil_image.close()
    if not bold_fonts:
        return []
    patched = []
    for line in lines:
        if not any(_pdf_font_metadata(char) in bold_fonts for char in line.chars):
            continue
        chars = [
            dict(char, font=dict(char["font"], weight=700)) if _pdf_font_metadata(char) in bold_fonts else char
            for char in line.chars
        ]
        patched.append(replace(line, chars=chars))
    return detect_pdf_text_style_lines(patched, [])
