"""验证宽松定界符不会把独立正文裁进原生公式图。"""

from dataclasses import replace

import pytest

from docvortex.analyzers.native.pdf.formulas import _capture_formula_crop_ink, _tighten_spatial_formula_crop
from docvortex.analyzers.native.pdf.models import _LineItem


def _line(index, text, bbox, tight=None):
    """构造保留真实紧框的原生行；松框和紧框可独立改变。"""
    return _LineItem(text, bbox, 0, index, chars=[{"char": text, "bbox": bbox, "tight_bbox": tight or bbox}])


@pytest.mark.parametrize("existing_tight_box", [False, True])
def test_spatial_formula_crop_excludes_unrelated_prose_without_changing_members(existing_tight_box):
    """异常松框延伸到整行正文时，仅改变公式的输出纵边。"""
    anchor = _line(1, "x+y", (160, 105, 185, 112))
    bracket = _line(2, ")", (156, 92, 160, 132), (156, 103, 160, 115))
    body = _line(3, "The following line explains the procedure.", (130, 122, 200, 129))
    lines = [anchor, bracket, body]
    crop = _capture_formula_crop_ink(lines)
    assert crop is not None
    for line in lines:
        line.chars.clear()
    members = [(item, item.bbox) for item in (anchor, bracket)]
    block = {"type": "equation", "bbox": (156, 92, 185, 132), "content": "x+y)", "_formula_members": [1, 2]}
    if existing_tight_box:
        block["_tight_output_bbox"] = (157, 92, 182, 131)

    _tighten_spatial_formula_crop(block, members, anchor, crop)

    expected_x = (157, 182) if existing_tight_box else (156, 185)
    assert block == {
        "type": "equation",
        "bbox": (156, 92, 185, 132),
        "content": "x+y)",
        "_formula_members": [1, 2],
        "_tight_output_bbox": (expected_x[0], 102.25, expected_x[1], 115.75),
    }


@pytest.mark.parametrize("variant", ["numbered", "unrelated_column", "no_prose", "unknown_ink", "changed_member"])
def test_spatial_formula_crop_keeps_existing_bounds_without_complete_evidence(variant):
    """保护编号真显示式、邻栏，及墨迹来源不完整或已重新聚类的成员。"""
    anchor = _line(1, "x+y (A1)" if variant == "numbered" else "x+y", (160, 105, 185, 112))
    bracket = _line(2, ")", (156, 92, 160, 132), (156, 103, 160, 115))
    body = _line(
        3,
        "x+y" if variant == "no_prose" else "The following line explains the procedure.",
        (250, 122, 320, 129) if variant == "unrelated_column" else (130, 122, 200, 129),
    )
    if variant == "unknown_ink":
        anchor.chars[0]["tight_bbox"] = None
    crop = _capture_formula_crop_ink([anchor, bracket, body])
    assert crop is not None
    member = replace(anchor, text="x+y+z") if variant == "changed_member" else anchor
    block = {"bbox": (156, 92, 185, 132)}

    _tighten_spatial_formula_crop(block, [(member, member.bbox), (bracket, bracket.bbox)], member, crop)

    assert block == {"bbox": (156, 92, 185, 132)}


def test_formula_crop_does_not_store_normal_delimiter_or_generated_ink():
    """正常定界符和引擎生成字符不会让普通页面持有额外墨迹记录。"""
    normal = _line(1, ")", (156, 100, 160, 115), (156, 103, 160, 115))
    synthetic = _line(2, "]", (156, 92, 160, 132), (156, 103, 160, 115))
    synthetic.chars[0]["is_generated"] = True
    assert _capture_formula_crop_ink([normal, synthetic]) is None
