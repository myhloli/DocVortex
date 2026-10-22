"""提供 PDF 字符 loose/tight/origin 驱动的通用上下标几何分类。"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

from ....document.pdf.text._contracts import Char
from ....schema import BBox
from ...._compute_backend import get_native

SCRIPT_BODY_COMPARABLE_HEIGHT_RATIO = 0.9
SCRIPT_BASELINE_ABSOLUTE_TOLERANCE = 0.35
SCRIPT_BASELINE_LOOSE_HEIGHT_RATIO = 0.04
SCRIPT_ORIGIN_MIN_SHIFT_ABSOLUTE = 0.5
SCRIPT_ORIGIN_MIN_SHIFT_RATIO = 0.12
SCRIPT_TIGHT_HEIGHT_RATIO = 0.9
SCRIPT_STRONG_SHIFT_RATIO = 0.3
SCRIPT_STRONG_MAX_HEIGHT_RATIO = 1.1
SCRIPT_CONSENSUS_TIGHT_HEIGHT_RATIO = 0.88
SCRIPT_CONSENSUS_ORIGIN_SHIFT_RATIO = 0.22
SCRIPT_CONSENSUS_TIGHT_CENTER_SHIFT_RATIO = 0.3
SCRIPT_CONSENSUS_LOOSE_CENTER_SHIFT_RATIO = 0.15
SCRIPT_LOOSE_HEIGHT_ANOMALY_RATIO = 1.35
SCRIPT_COMPONENT_FORWARD_GAP_RATIO = 1.5
SCRIPT_COMPONENT_X_BACKTRACK_RATIO = 0.5
SCRIPT_COMPONENT_SEED_GAP_RATIO = 0.5
SCRIPT_COMPONENT_SEED_MAX_POSITION_DISTANCE = 4
SCRIPT_COMPONENT_MIN_OFFSET_RATIO = 0.08
SCRIPT_COMPONENT_MAX_HEIGHT_RATIO = 1.1
CONTROL_LINE_BREAK_CHARS = {"\r", "\n"}
_NUMERIC_SCRIPT_SEPARATORS = frozenset({",", "，", "-", "–", "—"})

ScriptRole = Literal["body", "sup", "sub"]
ScriptMarkRole = Literal["sup", "sub"]
_ScriptFontKey = tuple[str, int | None, int | None]


@dataclass(frozen=True, slots=True)
class ScriptCharFeature:
    """保存单个字符参与纯几何上下标判定所需的只读特征。"""

    index: int
    text: str
    loose_bbox: BBox
    tight_bbox: BBox | None
    origin: tuple[float, float] | None
    is_valid: bool
    is_body_anchor: bool

    @property
    def loose_height(self) -> float:
        """返回 loose bbox 高度。"""
        return self.loose_bbox[3] - self.loose_bbox[1]

    @property
    def loose_center_y(self) -> float:
        """返回 loose bbox 中心 y。"""
        return (self.loose_bbox[1] + self.loose_bbox[3]) / 2

    @property
    def tight_height(self) -> float:
        """返回 tight bbox 高度，无有效框时返回零。"""
        if self.tight_bbox is None:
            return 0.0
        return self.tight_bbox[3] - self.tight_bbox[1]

    @property
    def tight_center_y(self) -> float | None:
        """返回 tight bbox 中心 y。"""
        if self.tight_bbox is None:
            return None
        return (self.tight_bbox[1] + self.tight_bbox[3]) / 2


def _coerce_finite_bbox(value: Any) -> BBox | None:
    """把可迭代四元组收敛为合法有限 bbox。"""
    try:
        raw_bbox = getattr(value, "bbox", value)
        if raw_bbox is None or len(raw_bbox) != 4:
            return None
        bbox = tuple(float(item) for item in raw_bbox)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in bbox) or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return None
    return bbox  # type: ignore[return-value]


def _char_geometry_key(char: Char) -> int | None:
    """返回可用于 side-map 查询的合法 PDFium char_idx。"""
    char_idx = char.get("char_idx")
    if isinstance(char_idx, bool) or not isinstance(char_idx, int):
        return None
    return char_idx


def build_script_features(
    chars: list[Char],
    tight_bboxes: dict[int, BBox],
    origins: dict[int, tuple[float, float]],
    protected_body_indices: set[int],
) -> list[ScriptCharFeature]:
    """一次性构造 loose/tight/origin 三类字符几何特征。"""
    features = []
    for index, char in enumerate(chars):
        text = str(char.get("char", ""))
        char_idx = _char_geometry_key(char)
        loose_bbox = _coerce_finite_bbox(char.get("bbox")) or (0.0, 0.0, 0.0, 0.0)
        tight_bbox = _coerce_finite_bbox(tight_bboxes.get(char_idx)) if char_idx is not None else None
        raw_origin = origins.get(char_idx) if char_idx is not None else None
        origin = None
        if raw_origin is not None:
            try:
                candidate_origin = (float(raw_origin[0]), float(raw_origin[1]))
            except (IndexError, TypeError, ValueError):
                candidate_origin = None
            if candidate_origin is not None and all(math.isfinite(value) for value in candidate_origin):
                origin = candidate_origin
        is_valid = text not in CONTROL_LINE_BREAK_CHARS and not text.isspace() and tight_bbox is not None and origin is not None
        features.append(
            ScriptCharFeature(
                index=index,
                text=text,
                loose_bbox=loose_bbox,
                tight_bbox=tight_bbox,
                origin=origin,
                is_valid=is_valid,
                is_body_anchor=is_valid and text.isalnum() and index not in protected_body_indices,
            )
        )
    return features


def split_script_visual_components(features: list[ScriptCharFeature]) -> list[list[int]]:
    """按换行、x 回退和大间隙切分独立视觉组件。"""
    valid_heights = [feature.loose_height for feature in features if feature.loose_height > 0]
    scale = statistics.median(valid_heights) if valid_heights else 1.0
    components: list[list[int]] = []
    current: list[int] = []
    previous_visible: ScriptCharFeature | None = None
    for feature in features:
        if feature.text in CONTROL_LINE_BREAK_CHARS or feature.text.isspace():
            if current:
                components.append(current)
                current = []
            previous_visible = None
            continue
        if feature.is_valid and previous_visible is not None:
            x_backtrack = previous_visible.loose_bbox[0] - feature.loose_bbox[0]
            forward_gap = feature.loose_bbox[0] - previous_visible.loose_bbox[2]
            if (
                x_backtrack > scale * SCRIPT_COMPONENT_X_BACKTRACK_RATIO
                or forward_gap > scale * SCRIPT_COMPONENT_FORWARD_GAP_RATIO
            ):
                if current:
                    components.append(current)
                current = []
                previous_visible = None
        current.append(feature.index)
        if feature.is_valid:
            previous_visible = feature
    if current:
        components.append(current)
    return components


def _script_font_key(char: Char) -> _ScriptFontKey | None:
    """读取原始字体身份，不合并子集名称，也不把原始字号当作有效字形高度。"""
    font = char.get("font")
    if not isinstance(font, dict):
        return None
    name = font.get("name")
    if not isinstance(name, str) or not name:
        return None
    flags, weight = font.get("flags"), font.get("weight")
    return name, flags if type(flags) is int else None, weight if type(weight) is int else None


def paired_script_roles(
    chars: list[Char],
    roles: list[ScriptRole],
    tight_bboxes: dict[int, BBox],
    origins: dict[int, tuple[float, float]],
) -> dict[int, ScriptRole]:
    """确认同一基字符右侧横向叠放的上下标，排除分式和远距邻列。"""

    features = build_script_features(chars, tight_bboxes, origins, set())
    visible = [feature for feature in features if feature.text.isprintable() and not feature.text.isspace()]
    paired: dict[int, ScriptRole] = {}
    for position, (base, first, second) in enumerate(zip(visible, visible[1:], visible[2:])):
        if (
            not all(feature.is_valid and feature.text.isalnum() for feature in (base, first, second))
            or roles[base.index] != "body"
            or {roles[first.index], roles[second.index]} != {"sup", "sub"}
        ):
            continue
        assert base.tight_bbox is not None and base.origin is not None
        assert first.tight_bbox is not None and first.origin is not None
        assert second.tight_bbox is not None and second.origin is not None
        scale = base.tight_height
        if scale <= 0 or any(feature.tight_height > 0.8 * scale for feature in (first, second)):
            continue
        overlap = min(first.tight_bbox[2], second.tight_bbox[2]) - max(first.tight_bbox[0], second.tight_bbox[0])
        minimum_width = min(first.tight_bbox[2] - first.tight_bbox[0], second.tight_bbox[2] - second.tight_bbox[0])
        if overlap < 0.5 * minimum_width:
            continue
        if any(
            not -0.15 * scale <= feature.tight_bbox[0] - base.tight_bbox[2] <= 0.5 * scale
            or abs(feature.origin[1] - base.origin[1]) < 0.15 * scale
            or abs(feature.origin[1] - base.origin[1]) > scale
            for feature in (first, second)
        ):
            continue
        if (first.origin[1] - base.origin[1]) * (second.origin[1] - base.origin[1]) >= 0:
            continue
        # 本补证据只接纳完整单字符角标，不能只给 grid 等多字母角标的首字母加样式。
        following = visible[position + 3] if position + 3 < len(visible) else None
        if (
            following is not None
            and following.is_valid
            and following.text.isalnum()
            and following.tight_bbox is not None
            and following.origin is not None
            and following.tight_height <= 0.8 * scale
            and abs(following.origin[1] - second.origin[1]) <= 0.15 * scale
            and -0.15 * scale <= following.tight_bbox[0] - second.tight_bbox[2] <= 0.5 * scale
        ):
            continue
        paired[first.index] = roles[first.index]
        paired[second.index] = roles[second.index]
    return paired


def _numeric_superscript_indices(features, roles, protected_body_indices):
    """准备 Unicode 和保护标志，由原生共享内核确认数字引用位置。"""
    records = [
        (
            feature.loose_bbox,
            feature.tight_bbox,
            feature.origin,
            _native_text_flags(feature.text)
            | (int(feature.index in protected_body_indices) << 7)
            | (int(feature.is_valid) << 8),
            -1,
        )
        for feature in features
    ]
    return set(get_native().numeric_script_indices(records, [{"body": 0, "sup": 1, "sub": 2}[role] for role in roles]))


@lru_cache(maxsize=4096)
def _native_text_flags(text: str) -> int:
    """缓存 Unicode 字符类别值，不缓存文档对象或可变几何。"""
    local = len(text) == 1 and (
        (text.isascii() and text.isalnum()) or "０" <= text <= "９" or "Ａ" <= text <= "Ｚ" or "ａ" <= text <= "ｚ"
    )
    return (
        int(text.isspace())
        | (int(text in CONTROL_LINE_BREAK_CHARS) << 1)
        | (int(text.isalnum()) << 2)
        | (int(text.isdecimal()) << 3)
        | (int(local) << 4)
        | (int(text in {"/", "⁄"}) << 5)
        | (int(text in _NUMERIC_SCRIPT_SEPARATORS) << 6)
    )


def _native_script_inputs(chars, tight_bboxes, origins, protected):
    """打包一次调用的几何与 Python 字体等价类，保留异常输入的原校验语义。"""
    loose_boxes, tight_boxes, points, flags_list, font_ids = [], [], [], [], []
    fonts = {}
    for index, char in enumerate(chars):
        text = str(char.get("char", ""))
        char_idx = _char_geometry_key(char)
        loose = char.get("bbox")
        loose_boxes.append(getattr(loose, "bbox", loose))
        tight_boxes.append(tight_bboxes.get(char_idx) if char_idx is not None else None)
        raw_origin = origins.get(char_idx) if char_idx is not None else None
        origin = None
        if raw_origin is not None:
            try:
                candidate = (float(raw_origin[0]), float(raw_origin[1]))
            except (IndexError, TypeError, ValueError):
                candidate = None
            origin = candidate
        flags = _native_text_flags(text)
        flags |= int(index in protected) << 7
        font = _script_font_key(char)
        font_id = -1 if font is None else fonts.setdefault(font, len(fonts))
        points.append(origin)
        flags_list.append(flags)
        font_ids.append(font_id)
    return loose_boxes, tight_boxes, points, flags_list, font_ids


def classify_char_script_roles(
    chars: list[Char],
    *,
    tight_bboxes: dict[int, BBox],
    origins: dict[int, tuple[float, float]],
    protected_body_indices: set[int] | None = None,
) -> list[ScriptRole]:
    """Python 准备 Unicode 特征，原生内核统一完成视觉组件与上下标分类。"""
    roles = get_native().script_roles_raw(
        *_native_script_inputs(chars, tight_bboxes, origins, protected_body_indices or set()), _coerce_finite_bbox
    )
    if roles is None:
        raise ValueError("native script classification rejected normalized characters")
    names = ("body", "sup", "sub")
    return [names[role] for role in roles]


__all__ = [
    "CONTROL_LINE_BREAK_CHARS",
    "ScriptCharFeature",
    "ScriptRole",
    "build_script_features",
    "classify_char_script_roles",
    "split_script_visual_components",
]
