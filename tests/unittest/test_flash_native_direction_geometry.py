"""用空间变换与内容替换验证整行斜向过滤，不为某种文字设置保护分支。"""

from copy import deepcopy
from dataclasses import fields
import math

import pytest

from docvortex._compute_backend import get_native

from docvortex.analyzers.native.pdf import native_text
from docvortex.document.pdf.text import get_lines_from_chars
from docvortex.document.pdf.text._contracts import Bbox


def _row(angle, *, rotation=None, count=8, scale=1.0, offset=(100.0, 100.0), page_rotation=0, text="A"):
    """构造独立的原点、墨迹框和字形角度，分别控制整行倾斜与字形倾斜。"""
    rotation = angle if rotation is None else rotation
    radians = math.radians(angle)
    result = []
    for index in range(count):
        x = offset[0] + index * 10.0 * scale * math.cos(radians)
        y = offset[1] + index * 10.0 * scale * math.sin(radians)
        box = (x, y - 8.0 * scale, x + 7.0 * scale, y + 2.0 * scale)
        result.append(
            {
                "char": text,
                "char_idx": index,
                "origin": (x, y),
                "tight_bbox": box,
                "loose_bbox": box,
                "bbox": Bbox(list(box)),
                "rotation": math.radians(rotation - page_rotation),
                "font": {"name": "Generic-Regular", "size": 10.0 * scale, "flags": 0, "weight": 400},
            }
        )
    return result


@pytest.mark.parametrize("angle", (5, 20, 45, 70, 110, 160, 200, 315))
@pytest.mark.parametrize("page_rotation", (0, 90, 180, 270))
@pytest.mark.parametrize("scale", (0.5, 1.0, 2.0))
def test_whole_diagonal_geometry_is_invariant_to_rotation_translation_and_scale(angle, page_rotation, scale):
    """整行原点一致斜排时，页面方向、坐标和字号变化不改变移除成员集合。"""
    chars = _row(angle, scale=scale, page_rotation=page_rotation, offset=(1300, 1700))
    assert native_text._filter_native_direction_chars(chars, page_rotation=page_rotation) == []


@pytest.mark.parametrize("text", ("中", "x", "1", "=", "∑", "?"))
@pytest.mark.parametrize("font", ("Generic-Regular", "Symbol-Italic", "Different-Family"))
def test_diagonal_decision_does_not_read_text_or_font_name(text, font):
    """相同整行几何的中英文、数字、符号和不同字体名必须得到相同删除结果。"""
    chars = _row(20, text=text)
    for char in chars:
        char["font"]["name"] = font
    assert native_text._filter_native_direction_chars(chars) == []


@pytest.mark.parametrize("axis", (0, 90, 180, 270))
@pytest.mark.parametrize("shear", (-20, 10, 20))
def test_standard_baseline_with_sheared_glyphs_is_kept_without_semantic_protection(axis, shear):
    """整行基线正常时字形角度不提供删除证据，所有原字对象及顺序均原样保留。"""
    chars = _row(axis, rotation=axis + shear)
    assert native_text._filter_native_direction_chars(chars) is chars


@pytest.mark.parametrize("count", (1, 2, 3))
def test_short_diagonal_fragments_are_not_complete_line_proof(count):
    """单字、双字和短碎片缺少完整斜行证据，保留所有内容而不判断是否属于数学。"""
    chars = _row(20, count=count)
    assert native_text._filter_native_direction_chars(chars) is chars


def test_duplicate_paint_cannot_turn_two_origins_into_complete_line_evidence():
    """两处孤立字符的重复描画不提供四个独立原点，即使端点跨度较大也不删除。"""
    chars = _row(20, count=8)
    two = [chars[0], chars[3]]
    duplicated = [two[0], deepcopy(two[0]), two[1], deepcopy(two[1])]
    assert native_text._filter_native_direction_chars(duplicated) is duplicated


def test_spaces_follow_only_a_geometrically_proven_line():
    """空格不计入四字证明，只有自身原点和方向吻合的空格才随已证行移除。"""
    chars = _row(20)
    chars[2]["char"] = " "
    chars[5]["char"] = "\u3000"
    assert native_text._filter_native_direction_chars(chars) == []
    incomplete = _row(20, count=4)
    incomplete[1]["char"] = " "
    assert native_text._filter_native_direction_chars(incomplete) is incomplete
    unrelated = _row(20)
    unrelated[2]["char"] = " "
    unrelated[2]["origin"] = (10, 1000)
    assert native_text._filter_native_direction_chars(unrelated) == [unrelated[2]]


@pytest.mark.parametrize("change", ("missing_origin", "missing_ink", "nonfinite", "inconsistent_glyph_axis", "wide_band"))
def test_incomplete_or_inconsistent_geometric_evidence_does_not_delete(change):
    """原点、墨迹、方向一致性或窄基线不足时不删除，不能由其他缓存或文字补证。"""
    chars = _row(20, count=6)
    if change == "missing_origin":
        chars[2]["origin"] = None
    elif change == "missing_ink":
        chars[2]["tight_bbox"] = None
    elif change == "nonfinite":
        chars[2]["origin"] = (float("nan"), 0)
    elif change == "inconsistent_glyph_axis":
        for char in chars:
            char["rotation"] = math.radians(10)
    else:
        for index, char in enumerate(chars):
            x, y = char["origin"]
            char["origin"] = (x, y + (4 if index % 2 else -4))
    assert native_text._filter_native_direction_chars(chars) is chars


def test_mixed_coarse_line_removes_only_diagonal_members_and_rebuilds_bounds():
    """同一粗行混合正文和斜水印时按各自空间连续性裁决，不删除整条粗行。"""
    body = _row(0, offset=(30, 40), text="b")
    diagonal = _row(315, offset=(10, 150), text="w")
    for index, char in enumerate([*body, *diagonal]):
        char["char_idx"] = index
    spans = [
        {
            "text": "".join(char["char"] for char in group),
            "chars": group,
            "rotation": group[0]["rotation"],
            "bbox": (0, 0, 300, 300),
        }
        for group in (body, diagonal)
    ]
    original = {"rotation": 0, "bbox": (0, 0, 300, 300), "spans": spans}
    (rebuilt,) = native_text._filter_native_direction_pdf_lines([original], 0)
    assert rebuilt["bbox"] == (30, 32, 107, 42)
    assert [char for span in rebuilt["spans"] for char in span["chars"]] == body
    assert original["spans"] is spans
    assert original["bbox"] == (0, 0, 300, 300)


def test_superscript_and_separate_baselines_do_not_form_a_diagonal_line():
    """大小字符交错的分层排版不能用两端连线伪造整行斜向，不识别字符语义。"""
    chars = _row(0, rotation=10)
    for index, char in enumerate(chars):
        if index % 2:
            x, y = char["origin"]
            char["origin"] = (x, y - 5)
            char["font"]["size"] = 6
    assert native_text._filter_native_direction_chars(chars) is chars


@pytest.mark.parametrize("rotation", (0, 90, 180, 270))
def test_python_and_native_visual_builders_share_geometry_filter(rotation):
    """原始字列表经 Python 与 Rust 视觉组行后得到相同剩余成员和行字段。"""
    get_native()
    chars = [
        *_row(0, offset=(20, 30), page_rotation=rotation, text="b"),
        *_row(20, offset=(20, 100), page_rotation=rotation, text="w"),
    ]
    for index, char in enumerate(chars):
        char["char_idx"] = index
    before = deepcopy([(char["char_idx"], char["origin"], char["tight_bbox"]) for char in chars])
    angles = (0, 90, 180, 270)
    expected = native_text._build_native_line_items(
        get_lines_from_chars(chars), (400, 400), page_rotation=rotation, supported_angles=angles
    )
    actual = native_text._build_native_line_items_from_chars(chars, (400, 400), page_rotation=rotation, supported_angles=angles)
    assert {char["char_idx"] for line in actual for char in line.chars} == set(range(8))
    assert [{field.name: getattr(line, field.name) for field in fields(line) if field.compare} for line in actual] == [
        {field.name: getattr(line, field.name) for field in fields(line) if field.compare} for line in expected
    ]
    assert [(char["char_idx"], char["origin"], char["tight_bbox"]) for char in chars] == before
