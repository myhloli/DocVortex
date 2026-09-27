from __future__ import annotations

import hashlib
import json
from pathlib import Path

from docvortex.analyzers.native.pdf import pipeline
from docvortex.analyzers.native.pdf.char_geometry import (
    _line_loose_tier_offsets,
    apply_line_geometry_repairs,
    build_document_geometry_plan,
)
from docvortex.analyzers.native.pdf.models import _LineItem
from docvortex.analyzers.native.pdf.native_text import _resplit_native_visual_runs
from docvortex.document.pdf._document import PDFPageTextGeometry

_PDF_FIXTURE_XOR_KEY = b"MinerU flash layout fixture"


def _read_pdf_fixture(path: Path) -> bytes:
    """读取普通 PDF，或在内存中解密以 .xor 结尾的测试样本。"""

    payload = path.read_bytes()
    if path.suffix != ".xor":
        return payload
    key_length = len(_PDF_FIXTURE_XOR_KEY)
    return bytes(value ^ _PDF_FIXTURE_XOR_KEY[index % key_length] for index, value in enumerate(payload))


def _line_fixture(
    *,
    source_index: int,
    baseline: float,
    count: int = 40,
    advance: float = 6.0,
    loose_width: float = 6.0,
    loose_top: float | None = None,
    loose_bottom: float | None = None,
    font_name: str = "ABCDEF+Fixture",
    font_size: float = 10.0,
    start_char_idx: int = 0,
    split_baseline: float | None = None,
) -> tuple[_LineItem, PDFPageTextGeometry]:
    """构造 loose/tight/origin 均可控的单行几何 fixture。"""

    chars = []
    loose_bboxes = {}
    tight_bboxes = {}
    origins = {}
    top = baseline - 8.0 if loose_top is None else loose_top
    bottom = baseline + 2.0 if loose_bottom is None else loose_bottom
    for position in range(count):
        char_idx = start_char_idx + position
        origin_y = split_baseline if split_baseline is not None and position >= count // 2 else baseline
        origin_x = 10.0 + position * advance
        loose_bbox = (
            origin_x,
            top if origin_y == baseline else origin_y - 8.0,
            origin_x + loose_width,
            bottom if origin_y == baseline else origin_y + 2.0,
        )
        tight_bbox = (origin_x + 0.5, origin_y - 7.0, origin_x + 5.0, origin_y)
        chars.append(
            {
                "char": chr(ord("A") + position % 26),
                "bbox": loose_bbox,
                "rotation": 0.0,
                "font": {
                    "name": font_name,
                    "flags": 0,
                    "size": font_size,
                    "weight": 400,
                },
                "char_idx": char_idx,
            }
        )
        loose_bboxes[char_idx] = loose_bbox
        tight_bboxes[char_idx] = tight_bbox
        origins[char_idx] = (origin_x, origin_y)
    bbox = (
        min(value[0] for value in loose_bboxes.values()),
        min(value[1] for value in loose_bboxes.values()),
        max(value[2] for value in loose_bboxes.values()),
        max(value[3] for value in loose_bboxes.values()),
    )
    line = _LineItem(
        text="".join(char["char"] for char in chars),
        bbox=bbox,
        angle=0,
        source_index=source_index,
        chars=chars,  # type: ignore[arg-type]
        effective_height=bottom - top,
    )
    return line, PDFPageTextGeometry(
        chars=chars,  # type: ignore[arg-type]
        tight_bboxes=tight_bboxes,
        origins=origins,
        loose_bboxes=loose_bboxes,
    )


def _merge_geometries(*geometries: PDFPageTextGeometry) -> PDFPageTextGeometry:
    """合并同页多行 fixture 的字符 side-map。"""

    return PDFPageTextGeometry(
        chars=[char for geometry in geometries for char in geometry.chars],
        tight_bboxes={key: value for geometry in geometries for key, value in geometry.tight_bboxes.items()},
        origins={key: value for geometry in geometries for key, value in geometry.origins.items()},
        loose_bboxes={key: value for geometry in geometries for key, value in geometry.loose_bboxes.items()},
    )


def test_two_page_repeated_loose_height_inflation_sets_canonical_em_scale() -> None:
    """验证两页内重复的 loose 高度异常即可按字号与 tight 几何校准全文。"""

    lines_by_page: list[list[_LineItem]] = []
    geometries: list[PDFPageTextGeometry] = []
    for page_index in range(2):
        page_lines = []
        page_geometries = []
        for line_index in range(3):
            baseline = 20.0 + 16.0 * line_index
            anomalous = line_index < 2
            line, geometry = _line_fixture(
                source_index=line_index,
                baseline=baseline,
                loose_top=(baseline - 20.0 if anomalous else baseline - 8.0),
                loose_bottom=(baseline + 4.0 if anomalous else baseline + 2.0),
                loose_width=12.0 if anomalous else 6.0,
                font_name=("ABCDEF+Anomaly" if anomalous else "ABCDEF+Normal"),
                font_size=10.0,
                start_char_idx=(page_index * 10000 + line_index * 100),
            )
            page_lines.append(line)
            page_geometries.append(geometry)
        lines_by_page.append(page_lines)
        geometries.append(_merge_geometries(*page_geometries))

    plan = build_document_geometry_plan(
        lines_by_page,
        geometries,
        [(300.0, 200.0)] * 2,
    )
    for page_index, lines in enumerate(lines_by_page):
        apply_line_geometry_repairs(
            lines,
            page_index=page_index,
            plan=plan,
            allow_y_trim=True,
        )

    assert plan.document_style_anomaly
    assert all(lines[0].em_height == 10.0 for lines in lines_by_page)
    assert any(run["style_y_bad"] for run in plan.run_diagnostics)


def test_line_loose_tier_shrinks_repeated_largest_tier_to_second_tier() -> None:
    """验证同行重复最大 loose 档按次档的归一化 ascent/descent 回缩。"""

    offsets = _line_loose_tier_offsets(
        [(8.0, 2.0, 7.0, 10.0)] * 6 + [(20.0, 4.0, 7.0, 10.0)] * 4,
        10.0,
    )

    assert offsets == (8.0, 2.0)


def test_line_loose_tier_preserves_legitimate_mixed_font_sizes() -> None:
    """验证按各自 em 归一化后相同的真实混合字号不会形成异常高度档。"""

    offsets = _line_loose_tier_offsets(
        [(8.0, 2.0, 7.0, 10.0)] * 4 + [(16.0, 4.0, 14.0, 20.0)] * 4,
        10.0,
    )

    assert offsets is None


def test_line_loose_tier_ignores_single_large_outlier() -> None:
    """验证单个 loose 高度离群字符不足以触发整行档位回缩。"""

    offsets = _line_loose_tier_offsets(
        [(8.0, 2.0, 7.0, 10.0)] * 7 + [(20.0, 4.0, 7.0, 10.0)],
        10.0,
    )

    assert offsets is None


def test_strong_x_run_repairs_advance_and_contains_tight_bbox() -> None:
    """验证强 X 异常 run 按 origin advance 收缩且仍包含 tight。"""

    line, geometry = _line_fixture(source_index=0, baseline=20.0, loose_width=12.0)
    plan = build_document_geometry_plan([[line]], [geometry], [(400.0, 100.0)])

    assert len(plan.char_repairs) == 40
    repair = plan.char_repairs[(0, 0)]
    assert repair.x_state == "abnormal"
    assert repair.layout_bbox[2] <= geometry.origins[1][0]
    assert repair.layout_bbox[0] <= repair.tight_bbox[0]
    assert repair.layout_bbox[2] >= repair.tight_bbox[2]
    assert plan.line_repairs[(0, 0)].state == "repair_x"


def test_sparse_trailing_punctuation_inherits_confirmed_x_repair() -> None:
    """验证异常字体 run 尾部的稀疏标点使用同样式 donor 收紧字符和行框。"""

    line, geometry = _line_fixture(
        source_index=0,
        baseline=20.0,
        count=40,
        loose_width=12.0,
    )
    punctuation = line.chars[-1]
    char_idx = int(punctuation["char_idx"])
    origin_x, origin_y = geometry.origins[char_idx]
    inflated_bbox = (origin_x, 12.0, 400.0, 22.0)
    punctuation["char"] = "."
    punctuation["bbox"] = inflated_bbox
    geometry.loose_bboxes[char_idx] = inflated_bbox
    line.text = f"{line.text[:-1]}."
    line.bbox = (line.bbox[0], line.bbox[1], 400.0, line.bbox[3])

    plan = build_document_geometry_plan(
        [[line]],
        [geometry],
        [(500.0, 100.0)],
    )

    punctuation_repair = plan.char_repairs[(0, char_idx)]
    assert punctuation_repair.origin == (origin_x, origin_y)
    assert punctuation_repair.layout_bbox == (244.5, 12.0, 250.0, 22.0)
    assert punctuation_repair.layout_bbox[2] >= punctuation_repair.tight_bbox[2]
    assert plan.line_repairs[(0, 0)].layout_bbox == (10.5, 12.0, 250.0, 22.0)
    assert plan.line_repairs[(0, 0)].repaired_char_count == 40


def test_sparse_punctuation_fallback_requires_inflated_matching_style() -> None:
    """验证正常宽标点和不同字体标点不会仅因邻近异常字母而继承 donor。"""

    normal, normal_geometry = _line_fixture(
        source_index=0,
        baseline=20.0,
        count=40,
        loose_width=12.0,
    )
    normal_punctuation = normal.chars[-1]
    normal_idx = int(normal_punctuation["char_idx"])
    normal_origin_x, _normal_origin_y = normal_geometry.origins[normal_idx]
    normal_bbox = (normal_origin_x, 12.0, normal_origin_x + 6.0, 22.0)
    normal_punctuation["char"] = "."
    normal_punctuation["bbox"] = normal_bbox
    normal_geometry.loose_bboxes[normal_idx] = normal_bbox
    normal.text = f"{normal.text[:-1]}."
    normal.bbox = (normal.bbox[0], normal.bbox[1], normal_bbox[2], normal.bbox[3])

    different, different_geometry = _line_fixture(
        source_index=1,
        baseline=40.0,
        count=40,
        loose_width=12.0,
        start_char_idx=100,
    )
    different_punctuation = different.chars[-1]
    different_idx = int(different_punctuation["char_idx"])
    different_origin_x, _different_origin_y = different_geometry.origins[different_idx]
    different_bbox = (different_origin_x, 32.0, 400.0, 42.0)
    different_punctuation["char"] = "."
    different_punctuation["bbox"] = different_bbox
    different_punctuation["font"] = {
        "name": "ABCDEF+Different",
        "flags": 0,
        "size": 10.0,
        "weight": 400,
    }
    different_geometry.loose_bboxes[different_idx] = different_bbox
    different.text = f"{different.text[:-1]}."
    different.bbox = (different.bbox[0], different.bbox[1], 400.0, different.bbox[3])

    plan = build_document_geometry_plan(
        [[normal, different]],
        [_merge_geometries(normal_geometry, different_geometry)],
        [(500.0, 100.0)],
    )

    assert (0, normal_idx) not in plan.char_repairs
    assert (0, different_idx) not in plan.char_repairs


def test_sparse_column_boundary_glyph_repair_restores_native_resplit() -> None:
    """验证左栏末尾稀疏字符修复后恢复栏沟，使生产重切生成唯一来源序号。"""

    line, geometry = _line_fixture(
        source_index=0,
        baseline=20.0,
        count=80,
        loose_width=12.0,
    )
    for position in range(40, 80):
        char = line.chars[position]
        char_idx = int(char["char_idx"])
        source_bbox = geometry.loose_bboxes[char_idx]
        tight_bbox = geometry.tight_bboxes[char_idx]
        origin = geometry.origins[char_idx]
        shifted_source = (
            source_bbox[0] + 100.0,
            source_bbox[1],
            source_bbox[2] + 100.0,
            source_bbox[3],
        )
        char["bbox"] = shifted_source
        char["font"] = {
            "name": "ABCDEF+RightColumn",
            "flags": 0,
            "size": 10.0,
            "weight": 400,
        }
        geometry.loose_bboxes[char_idx] = shifted_source
        geometry.tight_bboxes[char_idx] = (
            tight_bbox[0] + 100.0,
            tight_bbox[1],
            tight_bbox[2] + 100.0,
            tight_bbox[3],
        )
        geometry.origins[char_idx] = (origin[0] + 100.0, origin[1])
    punctuation = line.chars[39]
    punctuation_idx = int(punctuation["char_idx"])
    punctuation_origin_x, _punctuation_origin_y = geometry.origins[punctuation_idx]
    punctuation_bbox = (punctuation_origin_x, 12.0, geometry.origins[40][0], 22.0)
    punctuation["char"] = "."
    punctuation["bbox"] = punctuation_bbox
    geometry.loose_bboxes[punctuation_idx] = punctuation_bbox
    line.text = f"{line.text[:39]}.{line.text[40:]}"
    line.bbox = (10.0, 12.0, max(bbox[2] for bbox in geometry.loose_bboxes.values()), 22.0)

    plan = build_document_geometry_plan(
        [[line]],
        [geometry],
        [(700.0, 100.0)],
    )
    apply_line_geometry_repairs(
        [line],
        page_index=0,
        plan=plan,
        allow_y_trim=False,
    )
    members, resplits = _resplit_native_visual_runs(
        [line],
        (700.0, 100.0),
        {char_idx: repair.layout_bbox for (page_index, char_idx), repair in plan.char_repairs.items() if page_index == 0},
        source_index_start=1,
    )

    assert (0, punctuation_idx) in plan.char_repairs
    assert [member.source_index for member in members] == [0, 1]
    assert [member.text for member in members] == [line.text[:40], line.text[40:]]
    assert list(resplits) == [0]


def test_short_rows_accumulate_enough_pairs_for_strong_x_repair() -> None:
    """验证大量三字符短行仍可累计文档级 X 异常证据。"""

    lines = []
    geometries = []
    for line_index in range(15):
        line, geometry = _line_fixture(
            source_index=line_index,
            baseline=10.0 + 5.0 * line_index,
            count=3,
            loose_width=12.0,
            start_char_idx=10 * line_index,
        )
        lines.append(line)
        geometries.append(geometry)

    plan = build_document_geometry_plan(
        [lines],
        [_merge_geometries(*geometries)],
        [(400.0, 100.0)],
    )

    assert any(run["reliable_pair_count"] == 30 and run["strong_x_bad"] for run in plan.run_diagnostics)
    assert plan.char_repairs
    assert all(plan.line_repairs[(0, line_index)].state == "repair_x" for line_index in range(15))


def test_style_only_prefilter_calibrates_scale_without_rewriting_output_geometry() -> None:
    """验证温和跨页高度异常只校准字号，不顺带启用公开 bbox 重写。"""

    lines_by_page = []
    geometries = []
    original_bboxes = []
    for page_index in range(2):
        page_lines = []
        page_geometries = []
        for line_index in range(3):
            baseline = 20.0 + 20.0 * line_index
            line, geometry = _line_fixture(
                source_index=line_index,
                baseline=baseline,
                count=6,
                font_size=7.0,
                loose_top=baseline - 11.3,
                loose_bottom=baseline + 2.0,
                start_char_idx=page_index * 100 + line_index * 10,
            )
            page_lines.append(line)
            page_geometries.append(geometry)
            original_bboxes.append(line.bbox)
        lines_by_page.append(page_lines)
        geometries.append(_merge_geometries(*page_geometries))

    plan = build_document_geometry_plan(
        lines_by_page,
        geometries,
        [(200.0, 100.0)] * 2,
    )
    for page_index, lines in enumerate(lines_by_page):
        apply_line_geometry_repairs(
            lines,
            page_index=page_index,
            plan=plan,
            allow_y_trim=True,
        )

    assert plan.document_style_anomaly
    assert plan.line_style_scales
    assert plan.line_ink_bboxes == {}
    assert plan.line_baselines == {}
    assert plan.line_repairs == {}
    assert [line.bbox for lines in lines_by_page for line in lines] == original_bboxes
    assert all(line.em_height == 7.0 for lines in lines_by_page for line in lines)


def test_style_prefilter_keeps_subthreshold_document_on_identity_path() -> None:
    """验证未超过 style inflation 阈值的跨页行保持完全 identity。"""

    lines_by_page = []
    geometries = []
    for page_index in range(2):
        page_lines = []
        page_geometries = []
        for line_index in range(3):
            baseline = 20.0 + 20.0 * line_index
            line, geometry = _line_fixture(
                source_index=line_index,
                baseline=baseline,
                count=6,
                font_size=7.0,
                loose_top=baseline - 10.5,
                loose_bottom=baseline + 2.0,
                start_char_idx=page_index * 100 + line_index * 10,
            )
            page_lines.append(line)
            page_geometries.append(geometry)
        lines_by_page.append(page_lines)
        geometries.append(_merge_geometries(*page_geometries))

    plan = build_document_geometry_plan(
        lines_by_page,
        geometries,
        [(200.0, 100.0)] * 2,
    )

    assert not plan.document_style_anomaly
    assert plan.line_style_scales == {}
    assert plan.run_diagnostics == []


def test_canonical_line_metrics_propagate_without_y_bbox_rewrite() -> None:
    """验证只发生 X 修复的行仍获得 tight 字形并集和 dominant origin 基线。"""

    line, geometry = _line_fixture(
        source_index=0,
        baseline=20.0,
        loose_width=12.0,
    )
    plan = build_document_geometry_plan(
        [[line]],
        [geometry],
        [(400.0, 100.0)],
    )

    apply_line_geometry_repairs(
        [line],
        page_index=0,
        plan=plan,
        allow_y_trim=False,
    )

    assert line.baseline == 20.0
    assert line.ink_bbox == (
        10.5,
        13.0,
        249.0,
        20.0,
    )
    assert line.geometry_state == "repair_x"


def test_normal_monospace_run_keeps_identity_geometry() -> None:
    """验证 fixed cell 与 origin advance 一致时不会被判为异常。"""

    line, geometry = _line_fixture(source_index=0, baseline=20.0, loose_width=6.0)
    plan = build_document_geometry_plan([[line]], [geometry], [(400.0, 100.0)])

    assert plan.char_repairs == {}
    assert (0, 0) not in plan.line_repairs


def test_zero_rotation_ignores_untrusted_loose_side_map_shadow() -> None:
    """验证零旋转字符只使用原始 bbox，不让密集 side-map 扰动改变布局计划。"""

    line, geometry = _line_fixture(source_index=0, baseline=20.0, loose_width=6.0)
    perturbed = {
        char_idx: (bbox[0], bbox[1] - 20.0, bbox[2] + 30.0, bbox[3] + 20.0) for char_idx, bbox in geometry.loose_bboxes.items()
    }
    geometry.loose_bboxes.clear()
    geometry.loose_bboxes.update(perturbed)

    plan = build_document_geometry_plan([[line]], [geometry], [(400.0, 100.0)])

    assert plan.char_repairs == {}
    assert plan.line_repairs == {}


def test_rotated_char_rejects_implausible_side_map_x_expansion() -> None:
    """验证旋转字符的 loose 宽度远超原始和 tight 框时回退稳定原始几何。"""

    line, geometry = _line_fixture(source_index=0, baseline=20.0, loose_width=6.0)
    for char in line.chars:
        char["rotation"] = 0.2
    perturbed = {char_idx: (bbox[0], bbox[1], bbox[2] + 30.0, bbox[3]) for char_idx, bbox in geometry.loose_bboxes.items()}
    geometry.loose_bboxes.clear()
    geometry.loose_bboxes.update(perturbed)

    plan = build_document_geometry_plan([[line]], [geometry], [(400.0, 100.0)])

    assert plan.char_repairs == {}
    assert plan.line_repairs == {}


def test_missing_extended_geometry_is_exact_identity() -> None:
    """验证 tight/origin 缺失时完全沿用 legacy loose 行。"""

    line, geometry = _line_fixture(source_index=0, baseline=20.0)
    empty = PDFPageTextGeometry(chars=geometry.chars, tight_bboxes={}, origins={}, loose_bboxes=geometry.loose_bboxes)
    original = line.bbox
    plan = build_document_geometry_plan([[line]], [empty], [(400.0, 100.0)])

    apply_line_geometry_repairs([line], page_index=0, plan=plan, allow_y_trim=True)

    assert line.bbox == original
    assert line.geometry_state == "healthy"


def test_repeated_neighbor_intrusion_trims_only_y() -> None:
    """验证同 run 多行 loose 侵入邻行 tight core 时仅裁剪 Y。"""

    lines = []
    geometries = []
    for index, baseline in enumerate((20.0, 32.0, 44.0, 56.0)):
        line, geometry = _line_fixture(
            source_index=index,
            baseline=baseline,
            count=12,
            loose_width=6.0,
            loose_top=baseline - 8.0,
            loose_bottom=baseline + 9.0,
            start_char_idx=index * 20,
        )
        lines.append(line)
        geometries.append(geometry)
    geometry = _merge_geometries(*geometries)
    plan = build_document_geometry_plan([lines], [geometry], [(200.0, 100.0)])

    trimmed = [repair for repair in plan.line_repairs.values() if repair.state == "trim_y"]
    assert len(trimmed) >= 3
    for repair in trimmed:
        assert repair.layout_bbox[0] == repair.source_bbox[0]
        assert repair.layout_bbox[2] == repair.source_bbox[2]
        assert repair.layout_bbox[3] - repair.layout_bbox[1] < repair.source_bbox[3] - repair.source_bbox[1]
        assert repair.ink_bbox is not None
        assert repair.layout_bbox[1] <= repair.ink_bbox[1]
        assert repair.layout_bbox[3] >= repair.ink_bbox[3]


def test_split_y_is_shadow_only() -> None:
    """验证多基线 legacy line 只记录 split 候选而不改变输出。"""

    line, geometry = _line_fixture(
        source_index=0,
        baseline=20.0,
        count=8,
        loose_width=6.0,
        split_baseline=32.0,
    )
    original = line.bbox
    plan = build_document_geometry_plan([[line]], [geometry], [(200.0, 100.0)])

    assert plan.line_repairs[(0, 0)].split_y_candidate is True
    apply_line_geometry_repairs([line], page_index=0, plan=plan, allow_y_trim=True)
    assert line.bbox == original


def test_y_trim_is_not_applied_to_formula_candidate() -> None:
    """验证公式候选行不会进入生产 Y trim。"""

    lines = []
    geometries = []
    for index, baseline in enumerate((20.0, 32.0, 44.0, 56.0)):
        line, geometry = _line_fixture(
            source_index=index,
            baseline=baseline,
            count=12,
            loose_bottom=baseline + 9.0,
            start_char_idx=index * 20,
        )
        line.formula_candidate_only = True
        lines.append(line)
        geometries.append(geometry)
    plan = build_document_geometry_plan([lines], [_merge_geometries(*geometries)], [(200.0, 100.0)])

    assert all(repair.state != "trim_y" for repair in plan.line_repairs.values())


def test_strong_bad_font_family_propagates_to_supported_sibling_run() -> None:
    """验证已确认异常字体族只向仍有 overlap 证据的 sibling 传播。"""

    strong, strong_geometry = _line_fixture(
        source_index=0,
        baseline=20.0,
        count=40,
        loose_width=12.0,
        font_name="ABCDEF+SharedFont",
        font_size=10.0,
    )
    sibling, sibling_geometry = _line_fixture(
        source_index=1,
        baseline=40.0,
        count=12,
        loose_width=7.2,
        font_name="UVWXYZ+SharedFont",
        font_size=12.0,
        start_char_idx=100,
    )
    plan = build_document_geometry_plan(
        [[strong, sibling]],
        [_merge_geometries(strong_geometry, sibling_geometry)],
        [(400.0, 100.0)],
    )

    diagnostics = {tuple(item["run_key"]): item for item in plan.run_diagnostics}
    strong_item = next(item for key, item in diagnostics.items() if key[1] == 10.0)
    sibling_item = next(item for key, item in diagnostics.items() if key[1] == 12.0)
    assert strong_item["strong_x_bad"] is True
    assert sibling_item["sibling_x_bad"] is True
    assert (0, 100) in plan.char_repairs


def test_versioned_mixed_text_fixture_keeps_normal_geometry_identity() -> None:
    """验证版本化中英混排与等宽字体样本不会误触发生产修复。"""

    project_root = Path(__file__).parents[2]
    fixture = project_root / "tests" / "unittest" / "pdfs" / "flash_layout" / "mixed_text_layout_sample.pdf.xor"
    encrypted_bytes = fixture.read_bytes()
    assert hashlib.sha256(encrypted_bytes).hexdigest() == "fc3150c68c9f88b78d10dd2c321871f63a2e08be2099528fe00cf62d8a17e3d3"
    assert not encrypted_bytes.startswith(b"%PDF-")
    pdf_bytes = _read_pdf_fixture(fixture)
    assert hashlib.sha256(pdf_bytes).hexdigest() == "87d98f6f4e42152c38b0a1f8dcfefa3e5ac163cc086a6f37169cabadf77bd107"

    diagnostics: list[dict[str, object]] = []
    with pipeline.PDFDocument(pdf_bytes) as document:
        pages = pipeline._analyze_native_document(document, geometry_diagnostics=diagnostics)  # noqa: SLF001

    geometry = diagnostics[0]
    assert not any(
        run["strong_x_bad"] or run["sibling_x_bad"]  # type: ignore[index]
        for run in geometry["run_diagnostics"]  # type: ignore[index]
    )
    assert not any(
        line["state"] != "healthy"  # type: ignore[index]
        for line in geometry["line_repairs"]  # type: ignore[index]
    )
    for page_index, section_text in (
        (4, "1 前言"),
        (5, "2 准备工作"),
        (8, "3 工具安装"),
        (20, "4 常见问题"),
        (22, "5 附录："),
    ):
        section = next(block for block in pages[page_index] if section_text in str(block.get("content")))
        security = next(block for block in pages[page_index] if "文档密级：秘密" in str(block.get("content")))
        assert section["type"] == "paragraph_title"
        assert security["type"] == "header"


def test_flash_layout_manifest_uses_portable_repository_paths() -> None:
    """验证版本化 layout manifest 不保存主机绝对路径且页数完整。"""

    project_root = Path(__file__).parents[2]
    payload = json.loads(
        (project_root / "tests" / "fixtures" / "flash_layout_geometry_manifest.json").read_text(encoding="utf-8")
    )
    assert payload["schema_version"] == 1
    assert len(payload["documents"]) == 19
    assert sum(len(document["pages"]) for document in payload["documents"]) == 168
    for document in payload["documents"]:
        path = Path(document["path"])
        assert not path.is_absolute()
        assert ".." not in path.parts
        assert hashlib.sha256((project_root / path).read_bytes()).hexdigest() == document["sha256"], path


def test_native_document_risk_randomized_reference_parity() -> None:
    """随机组合跨页字号、混合 run、拆行和公式标志，逐项比较风险结论。"""
    import random
    import pytest
    from docvortex._compute_backend import get_native
    from docvortex.analyzers.native.pdf import char_geometry as rules

    if get_native() is None:
        pytest.skip("需要原生扩展")
    rng = random.Random(1729)
    for _case in range(120):
        pages, geometries, sizes = [], [], []
        for page in range(rng.randint(1, 3)):
            lines = []
            geometry = PDFPageTextGeometry(chars=[], tight_bboxes={}, origins={}, loose_bboxes={})
            for index in range(rng.randint(1, 5)):
                baseline = 30.0 + index * 45
                line, data = _line_fixture(
                    source_index=index,
                    baseline=baseline,
                    count=rng.randint(1, 44),
                    advance=rng.choice([4.0, 6.0, 9.0]),
                    loose_width=rng.choice([5.0, 9.0, 14.0]),
                    loose_top=baseline - rng.choice([8.0, 20.0, 35.0]),
                    font_name=rng.choice(["FixtureA", "FixtureB"]),
                    font_size=rng.choice([5.0, 10.0, 20.0]),
                    start_char_idx=index * 100,
                    split_baseline=baseline + 12 if rng.random() < 0.15 else None,
                )
                line.formula_candidate_only = rng.random() < 0.1
                line.restored_inline_cluster = rng.random() < 0.1
                line.compact_formula_cluster = rng.random() < 0.1
                for char in line.chars:
                    if rng.random() < 0.15:
                        char["font"] = dict(char["font"], name="Other")
                lines.append(line)
                geometry.chars.extend(data.chars)
                geometry.tight_bboxes.update(data.tight_bboxes)
                geometry.origins.update(data.origins)
                geometry.loose_bboxes.update(data.loose_bboxes)
            pages.append(lines)
            geometries.append(geometry)
            sizes.append((600.0, 400.0))
        assert rules._document_requires_full_geometry(pages, geometries, sizes) == (
            rules._document_requires_full_geometry_python(pages, geometries, sizes)
        )
        from copy import deepcopy

        owned = rules._prepare_layout_document(*deepcopy((pages, geometries, sizes)))
        samples, by_line = rules._collect_samples(pages, geometries, sizes)
        actual_runs = rules._build_run_stats(samples, by_line)
        expected_runs = rules._build_run_stats_python(samples, by_line)
        assert list(actual_runs) == list(expected_runs)
        if owned is not None:
            owned_samples, owned_lines, owned_runs, owned_inflated, owned_scales = owned
            reference_samples, reference_lines = deepcopy((samples, by_line))
            reference_runs = rules._build_run_stats_python(reference_samples, reference_lines)
            reference_inflated = rules._mark_style_inflated_runs(reference_runs)
            reference_plan = rules.DocumentGeometryPlan()
            rules._apply_style_scale_repairs(reference_plan, reference_inflated, reference_lines)
            if reference_inflated:
                rules._restore_stable_legacy_source_bboxes(reference_lines, sizes)
                reference_runs = rules._build_run_stats_python(reference_samples, reference_lines)
                for run in reference_runs.values():
                    run.style_y_bad = run.key in reference_inflated
            assert owned_samples == reference_samples
            assert owned_lines == reference_lines
            assert list(owned_runs) == list(reference_runs)
            assert owned_runs == reference_runs
            assert owned_inflated == reference_inflated
            assert owned_scales == reference_plan.line_style_scales
        for key, run in actual_runs.items():
            assert run == expected_runs[key]
        plan = rules.DocumentGeometryPlan()
        styled_runs, inflated, restored = rules._prepare_run_style(plan, samples, by_line)
        expected_inflated = rules._mark_style_inflated_runs(expected_runs)
        expected_plan = rules.DocumentGeometryPlan(
            style_inflated_runs=expected_inflated, document_style_anomaly=bool(expected_inflated)
        )
        rules._apply_style_scale_repairs(expected_plan, expected_inflated, by_line)
        assert styled_runs == expected_runs
        assert inflated == expected_inflated
        assert plan == expected_plan


def test_native_document_risk_custom_threshold_uses_reference(monkeypatch) -> None:
    """自定义阈值必须命中参考实现，不能静默使用 Rust 固定配置。"""
    from docvortex.analyzers.native.pdf import char_geometry as rules

    expected = rules._DocumentGeometryRisk(style=True)
    monkeypatch.setattr(rules, "X_RELIABLE_PAIR_MIN", 7)
    monkeypatch.setattr(rules, "_document_requires_full_geometry_python", lambda *args: expected)
    assert rules._document_requires_full_geometry([], [], []) is expected


def test_native_document_risk_failure_is_not_silently_retried(monkeypatch) -> None:
    """原生计算异常直接传播；只有明确不支持的输入允许参考计算。"""
    import pytest
    from docvortex import _compute_backend
    from docvortex.analyzers.native.pdf import char_geometry as rules

    class FailedNative:
        """模拟原生构造失败，排除静默回退。"""

        @staticmethod
        def NativeGeometryRisk():
            """抛出计算错误。"""
            raise RuntimeError("risk failure")

    monkeypatch.setattr(_compute_backend, "get_native", lambda: FailedNative)
    with pytest.raises(RuntimeError, match="risk failure"):
        rules._document_requires_full_geometry([], [], [])


def test_native_run_statistics_consumes_standard_samples(monkeypatch) -> None:
    """确认标准几何实际进入原生批次，并保持所有成员对象身份。"""
    import pytest
    from docvortex._compute_backend import get_native
    from docvortex.analyzers.native.pdf import char_geometry as rules

    if get_native() is None:
        pytest.skip("需要原生扩展")
    line, geometry = _line_fixture(source_index=0, baseline=30.0, loose_width=14.0)
    samples, by_line = rules._collect_samples([[line]], [geometry], [(600.0, 100.0)])
    expected = rules._build_run_stats_python(samples, by_line)

    def rejected_reference(*args):
        """标准样本若回退则测试失败，避免差分只覆盖同一参考实现。"""
        raise AssertionError("unexpected reference fallback")

    monkeypatch.setattr(rules, "_build_run_stats_python", rejected_reference)
    actual = rules._build_run_stats(samples, by_line)
    assert actual == expected
    assert next(iter(actual.values())).strong_x_bad
    assert all(a is b for a, b in zip(next(iter(actual.values())).samples, samples, strict=True))


def test_native_run_statistics_duplicate_members_use_reference() -> None:
    """别名样本不能按唯一索引表达时保留原成员重复次数与统计结果。"""
    from docvortex.analyzers.native.pdf import char_geometry as rules

    line, geometry = _line_fixture(source_index=0, baseline=30.0)
    samples, by_line = rules._collect_samples([[line]], [geometry], [(600.0, 100.0)])
    samples.append(samples[0])
    assert rules._build_run_stats(samples, by_line) == rules._build_run_stats_python(samples, by_line)


def _cross_page_style_samples():
    """构造跨页异常字体、健康行以及并列主字体，用于验证全文校准的传播。"""
    from collections import defaultdict
    from docvortex.analyzers.native.pdf import char_geometry as rules

    samples, by_line = [], defaultdict(list)
    for page in range(2):
        for source in range(2):
            line, geometry = _line_fixture(source_index=source, baseline=30.0, loose_top=1.0, loose_bottom=42.0, count=8)
            part, _ = rules._collect_samples([[line]], [geometry], [(600.0, 100.0)])
            for sample in part:
                sample.page_index = page
            samples.extend(part)
            by_line[(page, source)].extend(part)
    line, geometry = _line_fixture(source_index=9, baseline=30.0, count=4)
    for index, char in enumerate(line.chars):
        char["font"] = dict(char["font"], name="TieA" if index in (0, 3) else "TieB", size=10.0 if index in (0, 3) else 20.0)
    part, _ = rules._collect_samples([[line]], [geometry], [(600.0, 100.0)])
    samples.extend(part)
    by_line[(0, 9)].extend(part)
    return samples, by_line


def test_native_style_propagates_globally_and_preserves_ties(monkeypatch) -> None:
    """异常字体触发全文校准，健康行也校准，主字体并列仍选择首见者。"""
    import pytest
    from docvortex._compute_backend import get_native
    from docvortex.analyzers.native.pdf import char_geometry as rules

    if get_native() is None:
        pytest.skip("需要原生扩展")
    samples, by_line = _cross_page_style_samples()

    def rejected_reference(*args):
        """标准样本不允许退回原 run 统计路径。"""
        raise AssertionError("unexpected reference fallback")

    monkeypatch.setattr(rules, "_build_run_stats_python", rejected_reference)
    plan = rules.DocumentGeometryPlan()
    runs, inflated, restored = rules._prepare_run_style(plan, samples, by_line)
    assert plan.document_style_anomaly and len(inflated) == 1
    assert plan.line_style_scales[(0, 9)] == 10.0
    assert len(plan.line_style_scales) == 5
    assert {key for key, run in runs.items() if run.style_y_bad} == inflated


def test_native_style_duplicate_sources_and_custom_rule(monkeypatch) -> None:
    """重复来源行按原集合统计，自定义全文开关仍按原先顺序调用。"""
    from docvortex.analyzers.native.pdf import char_geometry as rules

    samples, by_line = _cross_page_style_samples()
    for sample in samples:
        sample.page_index = 0
    plan = rules.DocumentGeometryPlan()
    _, inflated, restored = rules._prepare_run_style(plan, samples, by_line)
    assert not inflated and not plan.document_style_anomaly
    samples, by_line = _cross_page_style_samples()
    monkeypatch.setattr(rules, "_document_uses_global_style_calibration", lambda runs: False)
    plan = rules.DocumentGeometryPlan()
    _, inflated, restored = rules._prepare_run_style(plan, samples, by_line)
    assert inflated and not plan.document_style_anomaly
    assert plan.line_style_scales == {}


def test_native_style_restores_source_before_single_run_statistics() -> None:
    """全文样式恢复直接使用原始字符框，最终统计和所有回写样本与两遍参考计算相等。"""
    from copy import deepcopy
    from docvortex.analyzers.native.pdf import char_geometry as rules

    samples, by_line = _cross_page_style_samples()
    for sample in samples:
        box = sample.local_source_bbox
        sample.local_source_bbox = (box[0], box[1], box[2] + 40.0, box[3])
    reference_samples, reference_lines = deepcopy((samples, by_line))
    expected_runs = rules._build_run_stats_python(reference_samples, reference_lines)
    expected_inflated = rules._mark_style_inflated_runs(expected_runs)
    rules._restore_stable_legacy_source_bboxes(reference_lines, [(600.0, 100.0)] * 2)
    expected_runs = rules._build_run_stats_python(reference_samples, reference_lines)
    for run in expected_runs.values():
        run.style_y_bad = run.key in expected_inflated
    plan = rules.DocumentGeometryPlan()
    actual_runs, inflated, restored = rules._prepare_run_style(plan, samples, by_line, restore_pages=[(600.0, 100.0)] * 2)
    from docvortex._compute_backend import get_native

    if get_native() is not None:
        assert restored
    if not restored:
        # 无原生扩展的参考任务仍需验证原流程，不能把未恢复的中间态当作最终结果。
        rules._restore_stable_legacy_source_bboxes(by_line, [(600.0, 100.0)] * 2)
        actual_runs = rules._build_run_stats_python(samples, by_line)
        for run in actual_runs.values():
            run.style_y_bad = run.key in inflated
    assert inflated == expected_inflated
    assert actual_runs == expected_runs
    assert samples == reference_samples


def test_native_style_rejection_does_not_partially_restore_samples() -> None:
    """后部字符包含不支持的来源框时，原生拒绝不得提前修改前部样本。"""
    import pytest
    from docvortex._compute_backend import get_native
    from docvortex.analyzers.native.pdf import char_geometry as rules

    native = get_native()
    if native is None:
        pytest.skip("需要原生扩展")
    samples, by_line = _cross_page_style_samples()
    for sample in samples:
        box = sample.local_source_bbox
        sample.local_source_bbox = (box[0], box[1], box[2] + 40.0, box[3])
    samples[-1].line.chars[samples[-1].position]["bbox"] = [1.0, 2.0, 3.0]
    before = [(s.source_bbox, s.local_source_bbox) for s in samples]
    keys = list({sample.run_key for sample in samples})
    assert (
        native.build_geometry_style(
            samples, by_line, keys, rules._CharSample, rules._RunStats, rules._LineItem, [(600.0, 100.0)] * 2
        )
        is None
    )
    assert [(s.source_bbox, s.local_source_bbox) for s in samples] == before


def test_native_style_restore_respects_repeated_members_and_page_keys() -> None:
    """同一样本跨来源键重复出现时，依次使用映射页尺寸并保留最后一次恢复结果。"""
    import pytest
    from copy import deepcopy
    from docvortex._compute_backend import get_native
    from docvortex.analyzers.native.pdf import char_geometry as rules

    if get_native() is None:
        pytest.skip("需要原生扩展")
    samples, by_line = _cross_page_style_samples()
    by_line[(1, 99)] = [samples[0]]
    pages = [(600.0, 100.0), (15.0, 100.0)]
    reference_samples, reference_lines = deepcopy((samples, by_line))
    initial_runs = rules._build_run_stats_python(reference_samples, reference_lines)
    inflated = rules._mark_style_inflated_runs(initial_runs)
    rules._restore_stable_legacy_source_bboxes(reference_lines, pages)
    expected_runs = rules._build_run_stats_python(reference_samples, reference_lines)
    for run in expected_runs.values():
        run.style_y_bad = run.key in inflated
    plan = rules.DocumentGeometryPlan()
    actual_runs, actual_inflated, restored = rules._prepare_run_style(plan, samples, by_line, restore_pages=pages)
    assert restored and actual_inflated == inflated
    assert samples == reference_samples
    assert actual_runs == expected_runs


def _style_only_document_fixture():
    """构造仅触发样式风险的两页文档，正常源框不触发 X/Y 布局修复。"""
    pages, geometries = [], []
    for _page in range(2):
        lines = []
        geometry = PDFPageTextGeometry(chars=[], tight_bboxes={}, origins={}, loose_bboxes={})
        for source in range(2):
            baseline = 30.0 + source * 30.0
            line, part = _line_fixture(
                source_index=source,
                baseline=baseline,
                count=8,
                font_size=8.0,
                loose_top=baseline - 10.0,
                loose_bottom=baseline + 4.0,
                start_char_idx=source * 100,
            )
            lines.append(line)
            geometry.chars.extend(part.chars)
            geometry.tight_bboxes.update(part.tight_bboxes)
            geometry.origins.update(part.origins)
            geometry.loose_bboxes.update(part.loose_bboxes)
        pages.append(lines)
        geometries.append(geometry)
    return pages, geometries, [(600.0, 100.0)] * 2


def test_native_style_document_avoids_python_character_samples(monkeypatch) -> None:
    """样式文档必须直接消费 Rust 数据，完整计划与参考一致且不构造字符 dataclass。"""
    import pytest
    from copy import deepcopy
    from docvortex._compute_backend import get_native
    from docvortex.analyzers.native.pdf import char_geometry as rules

    if get_native() is None:
        pytest.skip("需要原生扩展")
    args = _style_only_document_fixture()
    reference = deepcopy(args)
    with monkeypatch.context() as context:
        context.setattr(rules, "_build_run_stats", rules._build_run_stats_python)
        expected = rules.build_document_geometry_plan(*reference)

    def rejected_sample(*args, **kwargs):
        """标准样式文档不得物化 Python 字符样本。"""
        raise AssertionError("Python character sample materialized")

    monkeypatch.setattr(rules._CharSample, "__init__", rejected_sample)
    actual = rules.build_document_geometry_plan(*args)
    assert actual == expected and actual.document_style_anomaly
    assert all(not g.loose_bboxes for g in args[1])


def test_native_style_document_consumes_once_and_rejects_partial_line() -> None:
    """构造器错误不追加半行，结束后不能继续读取或追加自有文档。"""
    import pytest
    from docvortex._compute_backend import get_native

    native = get_native()
    if native is None:
        pytest.skip("需要原生扩展")
    document = native.NativeStyleDocument()
    row = [(0, (1.0, 1.0, 8.0, 10.0), None, (1.0, 2.0, 7.0, 9.0), (1.0, 9.0), 0.0)]
    with pytest.raises(ValueError, match="count differs"):
        document.add_line(row, (100.0, 100.0), 0, 0, 0, 10.0, lambda positions: [])
    assert document.add_line(row, (100.0, 100.0), 0, 0, 0, 10.0, lambda positions: None) is False
    assert document.finish([]) == ([], [], [])
    with pytest.raises(ValueError, match="consumed"):
        document.finish([])
    with pytest.raises(ValueError, match="consumed"):
        document.add_line([], (100.0, 100.0), 0, 0, 0, 10.0, None)


def test_owned_layout_document_preserves_rotations_and_complete_records() -> None:
    """四种页面文字方向均保留完整字符和 run 字段，并只在最终边界物化一次。"""
    import pytest
    from copy import deepcopy
    from docvortex._compute_backend import get_native
    from docvortex.analyzers.native.pdf import char_geometry as rules

    if get_native() is None:
        pytest.skip("需要原生扩展")
    for angle in (0, 90, 180, 270):
        line, geometry = _line_fixture(source_index=0, baseline=30.0)
        line.angle = angle
        args = ([[line]], [geometry], [(600.0, 100.0)])
        reference = deepcopy(args)
        samples, by_line = rules._collect_samples(*reference)
        runs = rules._build_run_stats_python(samples, by_line)
        actual = rules._prepare_layout_document(*args)
        assert actual is not None
        assert actual == (samples, by_line, runs, set(), {})
        assert list(actual[2]) == list(runs)
        assert not geometry.loose_bboxes


def test_owned_document_rejects_custom_font_before_conversion() -> None:
    """拒绝含自定义字体转换的文档时不执行转换、不清理侧表，参考路径仍只执行原次数。"""
    import pytest
    from docvortex._compute_backend import get_native
    from docvortex.analyzers.native.pdf import char_geometry as rules

    if get_native() is None:
        pytest.skip("需要原生扩展")

    class FontSize:
        """记录自定义浮点转换是否被原生试探提前执行。"""

        calls = 0

        def __float__(self):
            """统计实际转换次数。"""
            self.calls += 1
            return 10.0

    value = FontSize()
    line, geometry = _line_fixture(source_index=0, baseline=30.0)
    for char in line.chars:
        char["font"]["size"] = value
    assert rules._prepare_layout_document([[line]], [geometry], [(600.0, 100.0)]) is None
    assert value.calls == 0 and geometry.loose_bboxes
    samples, _ = rules._collect_samples([[line]], [geometry], [(600.0, 100.0)])
    assert value.calls == len(samples)


def test_native_style_restoration_accepts_owned_bbox_container() -> None:
    """真实字符 Bbox 包装不能导致原生恢复半途退回参考路径。"""
    import pytest
    from docvortex._compute_backend import get_native
    from docvortex.analyzers.native.pdf import char_geometry as rules
    from docvortex.document.pdf.text._contracts import Bbox

    if get_native() is None:
        pytest.skip("需要原生扩展")
    samples, by_line = _cross_page_style_samples()
    for sample in samples:
        char = sample.line.chars[sample.position]
        char["bbox"] = Bbox(list(char["bbox"]))
    plan = rules.DocumentGeometryPlan()
    _, inflated, restored = rules._prepare_run_style(plan, samples, by_line, restore_pages=[(600.0, 100.0)] * 2)
    assert inflated and restored


def test_owned_line_metrics_match_reference_across_flags_and_baselines() -> None:
    """共享聚类后仍保留宽度/人数的不同主基线规则、旋转与重复来源行语义。"""
    import random
    from copy import deepcopy
    import pytest
    from docvortex._compute_backend import get_native
    from docvortex.analyzers.native.pdf import char_geometry as rules

    if get_native() is None:
        pytest.skip("需要原生扩展")
    rng = random.Random(1419)
    for case in range(120):
        pages, geometries, sizes = _style_only_document_fixture()
        for page, geometry in zip(pages, geometries):
            for line in page:
                line.angle = (0, 90, 180, 270)[case % 4]
                line.formula_candidate_only = case % 13 == 0
                line.compact_formula_cluster = case % 17 == 0
                line.restored_inline_cluster = case % 11 == 0
                if case % 7 == 0:
                    line.source_index = 0
                for position, char in enumerate(line.chars):
                    idx = char["char_idx"]
                    origin = geometry.origins[idx]
                    offset = rng.choice((0.0, 0.0, 0.0, 1.0, 10.0))
                    geometry.origins[idx] = (origin[0], origin[1] + offset)
                    tight = geometry.tight_bboxes[idx]
                    geometry.tight_bboxes[idx] = (
                        tight[0],
                        tight[1] + offset,
                        tight[0] + rng.choice((0.5, 4.5, 20.0)),
                        tight[3] + offset,
                    )
                    if case % 3 == 0:
                        box = char["bbox"]
                        char["bbox"] = (box[0], box[1] - 12.0, box[2], box[3] + 12.0)
        reference = deepcopy((pages, geometries, sizes))
        samples, by_line = rules._collect_samples(*reference)
        runs = rules._build_run_stats_python(samples, by_line)
        plan = rules.DocumentGeometryPlan()
        rules._record_line_canonical_metrics(plan, by_line)
        inflated = rules._mark_style_inflated_runs(runs)
        if inflated:
            rules._restore_stable_legacy_source_bboxes(by_line, sizes)
            runs = rules._build_run_stats_python(samples, by_line)
            for run in runs.values():
                run.style_y_bad = run.key in inflated
        risk = rules._has_document_y_risk(by_line)
        result = rules._prepare_layout_document(pages, geometries, sizes, with_metrics=True)
        assert result is not None
        metrics, reports = result[5]
        inks, baselines, actual_risk = metrics
        assert {key: tuple(box) for key, box in inks} == plan.line_ink_bboxes
        assert dict(baselines) == plan.line_baselines
        assert actual_risk == risk
        assert reports == rules._run_diagnostics(runs)
        assert bool(result[0]) == bool(risk or any(run.strong_x_bad or run.sibling_x_bad for run in runs.values()))


def test_owned_layout_without_repairs_avoids_character_materialization(monkeypatch) -> None:
    """布局准入后没有实际 X/Y 修复时，仍返回完整 canonical 计划且不构造字符对象。"""
    from copy import deepcopy
    import pytest
    from docvortex._compute_backend import get_native
    from docvortex.analyzers.native.pdf import char_geometry as rules

    if get_native() is None:
        pytest.skip("需要原生扩展")
    line, geometry = _line_fixture(source_index=0, baseline=30.0)
    args = ([[line]], [geometry], [(600.0, 100.0)])

    def full_layout(*args):
        """固定准入，以检查二级风险排除后是否消除字符物化。"""
        return rules._DocumentGeometryRisk(layout=True, style=False)

    monkeypatch.setattr(rules, "_document_requires_full_geometry", full_layout)
    with monkeypatch.context() as context:
        context.setattr(rules, "_build_run_stats", rules._build_run_stats_python)
        expected = rules.build_document_geometry_plan(*deepcopy(args))

    def reject_sample(*args, **kwargs):
        """此分支不应再调用 Python 字符构造器。"""
        raise AssertionError("Unexpected character materialization")

    monkeypatch.setattr(rules._CharSample, "__init__", reject_sample)
    actual = rules.build_document_geometry_plan(*args)
    assert actual == expected
    assert actual.line_baselines and actual.line_ink_bboxes
    assert not geometry.loose_bboxes
