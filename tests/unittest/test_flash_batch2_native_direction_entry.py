"""完整32页实捕原字验证入口方向过滤，同时保护自有快照的原始索引。"""

import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from docvortex._compute_backend import get_native

from docvortex.analyzers.native.pdf import native_text, pipeline
from docvortex.document.pdf.native_contracts import PDFPageTextGeometry
from _native_direction_fixtures import actual_direction_case


FIXTURE = Path(__file__).parent / "fixtures/batch2_auxiliary/v17_actual_diagonal_direction_sources.json.gz"
FIXTURE_SHA = "d19024254ca3cf939365ae12cfd0b74ada190e65707288391a81d19143b9f8ba"


@pytest.mark.parametrize("page", [1, 2, 16, 32])
@pytest.mark.parametrize("owned", [False, True])
def test_actual_full_source_drops_diagonal_rows_and_chars_before_layout(page, owned):
    """运输完整实际行/原字，在采集边界排斜水印；模拟快照只测试边界，不冒称重新解析PDF。"""
    from docvortex._compute_backend import get_native

    source = actual_direction_case(f"watermark_actual_direction_source_p{page}", FIXTURE, FIXTURE_SHA)
    raw = source.chars
    geometry = PDFPageTextGeometry(
        raw,
        {char["char_idx"]: char["tight_bbox"] for char in raw if char.get("tight_bbox") is not None},
        {char["char_idx"]: char["origin"] for char in raw if char.get("origin") is not None},
    )
    # 原件水印实捕为340°；p16正文另有17.74°仿斜体且origin严格水平，p32另有90°原字。
    # 维护期待按真实来源区分，不能误把所有非0°字当成水印。
    expected = [char for char in raw if abs(math.degrees(char.get("rotation") or 0) - 340) > 0.1]
    assert len(raw) > 2000 and len(expected) < len(raw)
    assert any(line.formula_candidate_only for line in source.lines)

    class Owner:
        """提供实际自有快照协议，保留原始完整字符位置，不模拟任何排版规则。"""

        def prepare_visual_evidence(self, *_args):
            """原字和已保存视觉行分别运输，行构造入口在下方明确替换。"""

            return geometry, get_native().prepare_visual_lines(raw, source.page_size, 0, (0.0, 90.0, 270.0))

        def prepare_script_evidence(self, _flags):
            """空脚本记录仍要求建立完整原字符身份映射。"""
            return ()

        def prepare_geometry_evidence(self):
            """空几何记录仍要求建立完整原字符身份映射。"""
            return ()

        def tight_space_indices(self):
            """此方向测试不额外提出字符间距提示。"""
            return ()

    snapshot = SimpleNamespace(
        page_size=source.page_size,
        rotation=0,
        image_infos=[],
        native_text=Owner() if owned else None,
        text_geometry=geometry,
        drawing_lines=[],
        signature_bboxes=[],
        form_bboxes=[],
        path_infos=[],
        form_infos=[],
        link_annotations=[],
    )

    class SavedPage:
        """只读运输整页实捕输入，不打开原PDF或将原件裁成局部页。"""

        page_count = 1

        def _extract_native_page(self, _index):
            """返回完整保存快照；真实原字次序和字典身份保持。"""
            return snapshot

    collected = pipeline._collect_document_sources(SavedPage())
    (result,) = collected.page_sources
    assert [id(char) for char in result.chars] == [id(char) for char in expected]
    assert all(not line.formula_candidate_only for line in result.lines)
    allowed = {char["char_idx"] for char in expected}
    assert all(char["char_idx"] in allowed for line in result.lines for char in line.chars)
    # 紧凑Source可过滤，owner的原始几何/脚本下标必须仍指完整原字序列。
    assert collected.page_text_geometries[0].chars is raw
    if owned:
        identities = {id(char): index for index, char in enumerate(raw)}
        assert collected.page_owned_scripts[0][1] == identities
        assert collected.page_geometry_evidence[0][1] == identities


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
@pytest.mark.parametrize("prefix", ["\x02", " ", " \x02"])
@pytest.mark.parametrize("oblique", [False, True])
def test_empty_oblique_candidates_do_not_shift_native_visual_row_identity(angle, prefix, oblique):
    """无可靠墨迹的空行不参与几何删除，保留两条路径既有编号契约。"""
    from dataclasses import fields
    from docvortex.document.pdf.text import get_lines_from_chars
    from docvortex.document.pdf.text._contracts import Bbox

    get_native()
    chars = [
        {
            "char": text,
            "char_idx": index,
            "bbox": Bbox([index * 7.0, 0.0, index * 7.0 + 6, 10.0]),
            "rotation": 0.12 if oblique else math.radians(angle),
            "font": {"name": "test", "size": 10},
        }
        for index, text in enumerate(prefix)
    ]
    chars.append(
        {
            "char": "A",
            "char_idx": len(chars),
            "bbox": Bbox([0.0, 30.0, 6.0, 40.0]),
            "rotation": math.radians(angle),
            "font": {"name": "test", "size": 10},
        }
    )
    angles = (0, 90, 180, 270)
    expected = native_text._build_native_line_items(get_lines_from_chars(chars), (100, 100), supported_angles=angles)
    actual = native_text._build_native_line_items_from_chars(chars, (100, 100), supported_angles=angles)
    assert len(expected) == len(actual) == 1
    assert expected[0].text == actual[0].text == "A"
    # 几何过滤不能因缺少字符原点改变旧粗组行的空白/控制字符契约。
    assert expected[0].visual_row_id == actual[0].visual_row_id
    # 沿用既有差分的dataclass比较契约，缓存字段compare=False；原字Bbox保持身份，不深拷贝。
    assert [{field.name: getattr(line, field.name) for field in fields(line) if field.compare} for line in expected] == [
        {field.name: getattr(line, field.name) for field in fields(line) if field.compare} for line in actual
    ]
