"""按字形来源清理原生 PDF 重复文字，优先保护无法证明重复的内容。"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ._contracts import Bbox, Char
from ...._compute_backend import get_native

_GEOMETRY_EPSILON = 0.001
_MAX_OFFSET = 2.5
_TRANSLATION_EPSILON = 0.1
_MIN_OVERLAP = 0.45
_MAX_BUCKET_CANDIDATES = 64
Box = tuple[float, float, float, float]


@dataclass
class _Glyph:
    """保留一组可能来自同一字形的字符，组内绝不按相同字母去重。"""

    chars: list[Char]
    box: Box | None
    text: str
    angle: float

    @property
    def head(self) -> Char:
        """返回负责几何和绘制来源判断的代表字符。"""
        return self.chars[0]

    @property
    def signature(self) -> tuple[Any, ...]:
        """重复绘制必须具有相同文本、字体、方向和渲染方式。"""
        font = self.head.get("font") or {}
        return (
            self.text,
            *(font.get(k) for k in ("name", "flags", "size", "weight")),
            round(self.angle, 3),
            self.head.get("text_render_mode"),
            self.head.get("text_is_visible"),
        )


@lru_cache(maxsize=1)
def _radical_equivalents() -> dict[str, str]:
    """惰性读取固定 Unicode 数据，仅使用部首区间，不扩展为全局文字替换。"""
    path = Path(__file__).resolve().parents[3] / "resources/unicode/EquivalentUnifiedIdeograph-17.0.0.txt"
    mapping: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        source, target = (value.strip() for value in entry.split(";"))
        bounds = source.split("..")
        for code in range(int(bounds[0], 16), int(bounds[-1], 16) + 1):
            if 0x2E80 <= code <= 0x2FDF:
                mapping[chr(code)] = chr(int(target, 16))
    return mapping


def _is_han(text: str) -> bool:
    """仅识别统一汉字，避免把字母、符号或多字符文本当作汉字变体。"""
    return len(text) == 1 and unicodedata.name(text, "").startswith("CJK UNIFIED IDEOGRAPH-")


def _canonical_han(text: str) -> str:
    """对局部映射组中的单字符查询部首或兼容汉字等价关系。"""
    if len(text) != 1:
        return text
    if 0x2E80 <= ord(text) <= 0x2FDF:
        return _radical_equivalents().get(text, text)
    return unicodedata.normalize("NFKC", text)


def _source_indices(chars: list[Char]) -> tuple[int, ...]:
    """稳定合并原始索引，不依赖集合遍历顺序。"""
    return tuple(sorted({i for char in chars for i in char.get("source_indices", (char["char_idx"],))}))


def _mapping_groups(chars: list[Char]) -> list[_Glyph]:
    """由 Rust 计算映射保护区间，Python 保留 Unicode 等价判断和字形对象物化。"""
    rows = get_native().mapping_glyph_rows(chars, Bbox)
    if rows is None:
        raise TypeError("Invalid native character mapping records")
    result = []
    for first, last, box, angle in rows:
        group = chars[first:last]
        if last > first + 1 and len({char["char"] for char in group}) > 1:
            equivalents = {_canonical_han(char["char"]) for char in group}
            if len(equivalents) == 1 and _is_han(canonical := next(iter(equivalents))):
                replacement = group[0].copy()
                replacement["char"] = canonical
                replacement["source_indices"] = _source_indices(group)
                group = [replacement]
        text = group[0]["char"] if len(group) == 1 else "".join(char["char"] for char in group)
        result.append(_Glyph(group, box, text, angle))
    return result


def _project(box: Box, angle: float) -> Box:
    """将页面框投影到文字书写轴，统一处理横排和旋转文字。"""
    co, si = math.cos(angle), math.sin(angle)
    points = [(co * x + si * y, -si * x + co * y) for x in (box[0], box[2]) for y in (box[1], box[3])]
    return min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)


def _paint_pairs(glyphs: list[_Glyph]) -> tuple[list[tuple[int, int]], list[tuple[int, int, float, float]]]:
    """准备签名和对象编号，重复绘制候选统一由 Rust 计算。"""
    signatures, objects, records = {}, {}, []
    for glyph in glyphs:
        head = glyph.head
        obj, origin = head.get("text_object_id"), head.get("origin")
        eligible = (
            glyph.box is not None
            and bool(glyph.text.strip())
            and obj is not None
            and origin is not None
            and head.get("text_render_mode") in (0, 1, 2, 3, 4, 5, 6)
        )
        if eligible:
            object_id = objects.setdefault(obj, len(objects))
            signature = signatures.setdefault(glyph.signature, len(signatures))
            records.append((glyph.box, origin, signature, object_id, True))
        else:
            records.append((None, None, 0, 0, False))
    return get_native().paint_pairs(records)


def _components(count: int, pairs: list[tuple[int, int]]) -> list[int]:
    """由 Rust 并查集合并来源，保持最早字形及既有压缩顺序。"""
    return get_native().dedup_components(count, pairs)


def _confirmed_offsets(
    glyphs: list[_Glyph], pairs: list[tuple[int, int, float, float]], exact: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """准备宿主数学函数结果后由 Rust 确认连续平移证据。"""
    if not pairs:
        return []
    active = {p[0] for p in pairs}
    texts, angles, records = {}, {}, []
    for index, glyph in enumerate(glyphs):
        text_id = texts.setdefault(glyph.text, len(texts))
        projected, normal, angle_id = None, 0.0, 0
        if index in active:
            origin = glyph.head["origin"]
            normal = -math.sin(glyph.angle) * origin[0] + math.cos(glyph.angle) * origin[1]
            angle_id = angles.setdefault(round(glyph.angle * 1000), len(angles))
            projected = _project(glyph.box, glyph.angle)
        records.append((projected, normal, angle_id, text_id, glyph.text.isspace()))
    return get_native().confirmed_offsets(records, pairs, exact)


def _merge_sources(retained: _Glyph, duplicate: _Glyph) -> None:
    """复制代表字符后合并来源，避免修改调用方的原始字符记录。"""
    if len(retained.chars) == len(duplicate.chars):
        merged = []
        for a, b in zip(retained.chars, duplicate.chars):
            char = a.copy()
            char["source_indices"] = _source_indices([a, b])
            merged.append(char)
        retained.chars = merged
    else:
        retained.chars = [c.copy() for c in retained.chars]
        retained.chars[0]["source_indices"] = _source_indices(retained.chars + duplicate.chars)


def _collapse_paints(glyphs: list[_Glyph]) -> list[_Glyph]:
    """按连通的重复关系保留最早来源，传递合并三层以上的重复绘制。"""
    exact, offsets = _paint_pairs(glyphs)
    parents = _components(len(glyphs), exact + _confirmed_offsets(glyphs, offsets, exact))
    for index in range(len(glyphs) - 1, -1, -1):
        root = parents[index]
        if root != index:
            _merge_sources(glyphs[root], glyphs[index])
    return _retained_glyphs(glyphs, {index for index in range(len(glyphs)) if parents[index] != index})


def _retained_glyphs(glyphs: list[_Glyph], removed: set[int]) -> list[_Glyph]:
    """清除两侧内容都已删除的孤立空白，保留正常正文之间的空格与换行。"""
    if not removed:
        return glyphs
    trailing_removed = [True] * len(glyphs)
    following_removed = True
    for index in range(len(glyphs) - 1, -1, -1):
        trailing_removed[index] = following_removed
        if not glyphs[index].text.isspace():
            following_removed = index in removed
    result: list[_Glyph] = []
    previous_removed = True
    for index, glyph in enumerate(glyphs):
        orphaned = glyph.text.isspace() and previous_removed and trailing_removed[index]
        if not glyph.text.isspace():
            previous_removed = index in removed
        if index not in removed and not orphaned:
            result.append(glyph)
    return result


def _comparison_text(text: str) -> str:
    """仅在副本比较中统一空格和宽度形式，输出仍使用可见原文。"""
    return "".join(unicodedata.normalize("NFKC", text).split())


def _matches_hidden(hidden: str, visible: str) -> bool:
    """允许少量等长汉字 OCR 替换，数字、字母、增删内容均须严格一致。"""
    a, b = _comparison_text(hidden), _comparison_text(visible)
    if not a or len(a) != len(b):
        return False
    if a == b:
        return True
    matches = sum(x == y for x, y in zip(a, b))
    return matches >= 6 and matches / len(a) >= 0.85 and all(x == y or (_is_han(x) and _is_han(y)) for x, y in zip(a, b))


def _suppress_hidden(glyphs: list[_Glyph]) -> list[_Glyph]:
    """在 Rust 中筛选隐藏文本几何，Python 保持原 Unicode 匹配与复制规则。"""
    native = get_native()
    if not any(g.head.get("text_render_mode") == 3 for g in glyphs):
        return glyphs
    objects, records = {}, []
    indices = [glyph.head.get("char_idx") for glyph in glyphs]
    if not all(isinstance(index, int) for index in indices):
        raise TypeError("Invalid native hidden-text records")
    ranks = {index: rank for rank, index in enumerate(sorted(set(indices)))}
    for glyph, index in zip(glyphs, indices):
        head = glyph.head
        origin = head.get("origin")
        if origin is not None and len(origin) != 2:
            raise TypeError("Invalid native hidden-text records")
        try:
            obj = objects.setdefault(head.get("text_object_id"), len(objects))
        except TypeError:
            raise TypeError("Invalid native hidden-text records")
        mode = head.get("text_render_mode")
        mode = int(mode) if mode in (0, 1, 2, 3, 4, 5, 6) else -1
        angle = glyph.angle if math.isfinite(glyph.angle) else 0.0
        records.append(
            (
                glyph.box,
                origin,
                obj,
                mode,
                bool(head.get("text_is_visible", True)),
                bool(glyph.text.strip()),
                angle,
                math.cos(angle),
                math.sin(angle),
                ranks[index],
            )
        )
    candidates = native.hidden_candidates(records)
    if candidates is None:
        raise TypeError("Invalid native hidden-text records")
    removed = set()
    for run, matches in candidates:
        if not _matches_hidden("".join(glyphs[i].text for i in run), "".join(glyphs[i].text for i in matches)):
            continue
        removed.update(run)
        if len(run) == len(matches) and all(len(glyphs[a].chars) == len(glyphs[b].chars) for a, b in zip(run, matches)):
            for a, b in zip(run, matches):
                _merge_sources(glyphs[b], glyphs[a])
        else:
            representative = glyphs[matches[0]]
            representative.chars = [c.copy() for c in representative.chars]
            representative.chars[0]["source_indices"] = _source_indices(
                representative.chars + [c for i in run for c in glyphs[i].chars]
            )
    return _retained_glyphs(glyphs, removed)


def deduplicate_chars(chars: list[Char]) -> list[Char]:
    """统一入口：保护字形映射，再处理重复绘制及有可见对应的隐藏副本。"""
    if not chars:
        return []
    glyphs = _suppress_hidden(_collapse_paints(_mapping_groups(chars)))
    return [char for glyph in glyphs for char in glyph.chars]


__all__ = ["deduplicate_chars"]
