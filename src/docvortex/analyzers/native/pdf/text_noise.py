"""按原生绘制颜色和正文嵌套关系排除独立浅灰数字噪声，不依赖字符的具体内容或字体名称。"""

import re

from ...._compute_backend import get_native


def _numeric_noise_native(source, visual_bboxes):
    """当前输入状态一次计算文字特征，原生内核返回有序几何候选。"""
    native = get_native()
    lines = source.lines
    digits = [re.fullmatch(r"\d{1,3}", line.text.strip()) is not None for line in lines]
    eligible = [
        line.angle == 0 and line.semantic_type is None and bool(line.font_signature) and digit and line.effective_height > 0
        for line, digit in zip(lines, digits)
    ]
    if not any(eligible):
        return []
    groups, records = {}, []
    for line, digit, candidate in zip(lines, digits, eligible):
        font = groups.setdefault(line.font_signature, len(groups))
        english = len(re.findall(r"[A-Za-z]{2,}", line.text))
        chinese = len(re.findall(r"[\u3400-\u9fff]", line.text))
        records.append(
            (
                line.bbox,
                line.effective_height,
                line.angle == 0,
                bool(line.font_signature),
                font,
                digit,
                english + chinese >= 5,
                english >= 5 or chinese >= 12,
                re.match(r"\s*\d{1,2}[.)、]\s*[A-Za-z\u3400-\u9fff]", line.text) is not None,
                candidate,
            )
        )
    return [(lines[i], [lines[j] for j in refs]) for i, refs in native.numeric_noise_candidates(records, visual_bboxes)]


def _nested_prose_numeric_outliers(source, visual_bboxes=()):
    """原生批量匹配数值候选，返回同样的原始行对象与有序正文证据。"""
    return _numeric_noise_native(source, visual_bboxes)


def exclude_faint_native_noise(source, read_paint, visual_bboxes=()):
    """先证明嵌套的独立数字字体，再与深色正文或背景上的白色条目对照浅灰噪声；证据不足保持原文。"""
    candidates = _nested_prose_numeric_outliers(source, visual_bboxes)
    if not candidates or read_paint is None:
        return set()
    requested = {
        char["char_idx"]
        for line, prose in candidates
        for peer in [line, *prose]
        for char in peer.chars
        if char.get("char", "").strip() and "char_idx" in char
    }
    paint = read_paint(sorted(requested))
    excluded = set()
    for line, prose in candidates:
        colors = [paint.get(char.get("char_idx")) for char in line.chars if char.get("char", "").strip()]
        if not colors or any(
            color is None
            or color[3] == 0
            or min(color[:3]) < 170
            or max(color[:3]) > 225
            or max(color[:3]) - min(color[:3]) > 35
            for color in colors
        ):
            continue
        references = [paint.get(char.get("char_idx")) for peer in prose for char in peer.chars if char.get("char", "").strip()]
        references = [color for color in references if color and color[3] > 0]
        if references and (
            sum(0.2126 * color[0] + 0.7152 * color[1] + 0.0722 * color[2] <= 120 for color in references)
            >= 0.8 * len(references)
            or sum(min(color[:3]) >= 245 for color in references) >= 0.8 * len(references)
        ):
            excluded.add(line.source_index)
    if excluded:
        char_indices = {char.get("char_idx") for line in source.lines if line.source_index in excluded for char in line.chars}
        source.lines = [line for line in source.lines if line.source_index not in excluded]
        source.chars = [char for char in source.chars if char.get("char_idx") not in char_indices]
    return excluded
