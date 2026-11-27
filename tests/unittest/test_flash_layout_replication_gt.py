"""验证独立复制集的扩展字段、评分掩码与主集隔离。"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from tools import verify_flash_layout_gt as primary
from tools.verify_flash_layout_replication_gt import DEFAULT_GT, ReplicationGold, ReplicationPage, ReplicationRegion


def test_frozen_replication_cohorts() -> None:
    """读取完整的独立与主夹具，核对冻结样本、额外字段和跨集家族隔离。"""
    replication = ReplicationGold.model_validate_json(primary.read_json(DEFAULT_GT))
    main = primary.Gold.model_validate_json(primary.read_json(primary.DEFAULT_GT))

    assert len(replication.pages) == len({p.family_id for p in replication.pages}) == 27
    assert {p.family_id for p in replication.pages}.isdisjoint(p.family_id for p in main.pages)
    assert {p.pdf_sha256 for p in replication.pages}.isdisjoint(p.pdf_sha256 for p in main.pages)
    assert sum("table" in p.exhaustive_types for p in replication.pages) == 24
    regions = [r for p in replication.pages for r in p.regions]
    assert sum(r.type == "table" for r in regions) == 35
    assert sum(len(r.cell_assertions) for r in regions) == 44
    assert sum(bool(r.accepted_alternative_bboxes) for r in regions) == 6


def test_source_only_fixture_and_model_fingerprints() -> None:
    """核对第九轮的独立原件断言，并确保引用模型的标注仍须有相应权重指纹。"""
    blob = json.loads(primary.read_json(DEFAULT_GT.with_name("flash_layout_gt_r9_regions_v1.json.gz")))
    frozen = ReplicationGold.model_validate_json(json.dumps(blob))
    assert len(frozen.pages) == 36
    assert sum(len(p.protected_non_tables) for p in frozen.pages) == 14
    assert sum(len(r.cell_assertions) for p in frozen.pages for r in p.regions) == 218
    model_weights = {"pp": ["pp_weights_sha256"], "vlm": ["vlm_weights_sha256", "vlm_projector_sha256"]}
    for teacher, keys in model_weights.items():
        blob["pages"][0]["regions"][0].update(teacher=teacher, source_index=1)
        with pytest.raises(ValidationError, match="权重哈希"):
            ReplicationGold.model_validate_json(json.dumps(blob))
        ReplicationGold.model_validate_json(json.dumps({**blob, **dict.fromkeys(keys, "a" * 64)}))


def test_end_relative_row_requires_a_valid_anchor_and_extent() -> None:
    """从表尾定位时检查边界、跨度以及双重行坐标，允许表头的计行方式不同。"""
    from tools.verify_flash_layout_replication_gt import CellAssertion

    end = {"row_from_end": 1, "column_0based": 0, "text": "", "rowspan": 1, "colspan": 1}
    CellAssertion.model_validate(end)
    for change in ({"row_from_end": 0}, {"row_0based": 0}, {"rowspan": 2}):
        with pytest.raises(ValidationError):
            CellAssertion.model_validate({**end, **change})


def test_sparse_financial_fixtures_keep_source_pages_and_masks_separate() -> None:
    """确保已披露难例与新发行人各自保留原件、整格和邻表保护断言，不能重复计页。"""
    fixtures = []
    for suffix, pages, tables, cells in (("sparse", 13, 16, 251), ("sparse_holdout", 12, 23, 64)):
        path = DEFAULT_GT.with_name(f"flash_layout_gt_r10_{suffix}_v1.json.gz")
        gold = ReplicationGold.model_validate_json(primary.read_json(path))
        assert len(gold.pages) == pages
        assert all(page.source_archive_member and page.source_archive_member.startswith("PDF/") for page in gold.pages)
        regions = [region for page in gold.pages for region in page.regions if region.type == "table"]
        assert len(regions) == tables
        assert sum(len(region.cell_assertions) for region in regions) == cells
        assert any(page.forbidden_table_combinations for page in gold.pages)
        fixtures.append({page.pdf_sha256 for page in gold.pages})
    assert fixtures[0].isdisjoint(fixtures[1])


def test_replication_region_rejects_bad_geometry_and_cells() -> None:
    """防止错误的等价框、无掩码的单元格或越界的列在追加 GT 时静默通过。"""
    data = json.loads(primary.read_json(DEFAULT_GT))
    region = next(r for p in data["pages"] for r in p["regions"] if r.get("cell_assertions"))
    failures = (
        {**region, "accepted_alternative_bboxes": [[0.8, 0.2, 0.3, 0.4]]},
        {**region, "cell_assertions": []},
        {
            **region,
            "cell_assertions": [{"row_0based": None, "column_0based": region["table_dimensions"]["columns"], "text": "x"}],
        },
    )
    for invalid in failures:
        with pytest.raises(ValidationError):
            ReplicationRegion.model_validate_json(json.dumps(invalid))


def test_replication_page_rejects_foreign_guard_region() -> None:
    """确保局部表单保护关系只能引用当前物理页已标注的不同区域。"""
    data = json.loads(primary.read_json(DEFAULT_GT))
    page = next(p for p in data["pages"] if p.get("forbidden_table_combinations"))
    page["forbidden_table_combinations"][0]["covered_regions"].append("missing-region")
    with pytest.raises(ValidationError):
        ReplicationPage.model_validate_json(json.dumps(page))


def test_source_blank_cell_requires_position_and_existing_html_cell() -> None:
    """已确认的空格必须有确定行列且真实存在，占位空字符串与缺格应分别处理。"""
    from tools.score_flash_layout_replication import inspect_table
    from tools.verify_flash_layout_replication_gt import CellAssertion

    target = {"row_0based": 0, "column_0based": 1, "text": ""}
    CellAssertion.model_validate(target)
    for changed in ({**target, "row_0based": None}, {**target, "text": "  "}):
        with pytest.raises(ValidationError):
            CellAssertion.model_validate(changed)
    spec = {"cell_assertions": [target]}
    assert inspect_table("<table><tr><td>12</td><td></td></tr></table>", spec)["cells"][0]["pass"]
    assert not inspect_table("<table><tr><td>12</td></tr></table>", spec)["cells"][0]["pass"]
    assert not inspect_table("", spec)["cells"][0]["pass"]


def test_span_gold_requires_a_complete_in_bounds_rectangle() -> None:
    """跨度必须同时记录两个方向并在已确定的表格范围内锚定。"""
    data = json.loads(primary.read_json(DEFAULT_GT))
    region = next(r for p in data["pages"] for r in p["regions"] if r.get("cell_assertions"))
    base = {**region, "table_dimensions": {"rows": 3, "columns": 4}}
    cell = {"row_0based": 1, "column_0based": 2, "text": "header", "rowspan": 2, "colspan": 2}
    ReplicationRegion.model_validate_json(json.dumps({**base, "cell_assertions": [cell]}))
    for change in ({"rowspan": None}, {"row_0based": None}, {"rowspan": 0}, {"rowspan": 3}, {"colspan": 3}):
        with pytest.raises(ValidationError):
            ReplicationRegion.model_validate_json(json.dumps({**base, "cell_assertions": [{**cell, **change}]}))


def test_round_three_replication_families_and_source_masks() -> None:
    """第三轮新家族和前两轮严格分开，同时保留实际空格与只评列数的歧义表。"""
    third = ReplicationGold.model_validate_json(
        primary.read_json(DEFAULT_GT.with_name("flash_layout_gt_r3_replication_v1.json.gz"))
    )
    second = ReplicationGold.model_validate_json(primary.read_json(DEFAULT_GT))
    first = primary.Gold.model_validate_json(primary.read_json(primary.DEFAULT_GT))
    earlier = {p.pdf_sha256 for s in (first, second) for p in s.pages}
    assert len(third.pages) == len({p.family_id for p in third.pages}) == 20
    assert not earlier.intersection(p.pdf_sha256 for p in third.pages)
    assert all(p.exhaustive_types == ["table"] for p in third.pages)
    assert sum(p.visual_table_count == 0 for p in third.pages) == 5
    tables = [r for p in third.pages for r in p.regions if r.type == "table"]
    cells = [cell for r in tables for cell in r.cell_assertions]
    assert (len(tables), len(cells), sum(cell.text == "" for cell in cells)) == (31, 102, 3)
    assert sum(r.table_dimensions is not None and r.table_dimensions.rows is None for r in tables) == 6


def test_same_row_gold_requires_distinct_columns_and_a_mask() -> None:
    """同行断言至少含两个不同列且在已知宽度内，防止误把单列或越界数据计入分母。"""
    data = json.loads(primary.read_json(DEFAULT_GT))
    region = next(r for p in data["pages"] for r in p["regions"] if r.get("cell_assertions"))
    base = {
        **region,
        "table_dimensions": {"columns": 4},
        "cell_assertions": [],
        "scored_fields": ["bbox", "table_dimensions", "row_assertions"],
    }
    sample = [{"column_0based": 0, "text": "N"}, {"column_0based": 3, "text": "4"}]
    ReplicationRegion.model_validate_json(json.dumps({**base, "row_assertions": [sample]}))
    for invalid in ([sample[:1]], [sample + sample[:1]], [[sample[0], {**sample[1], "column_0based": 4}]], []):
        with pytest.raises(ValidationError):
            ReplicationRegion.model_validate_json(json.dumps({**base, "row_assertions": invalid}))


def test_fourth_replication_keeps_partial_financial_page_and_new_source_families() -> None:
    """确保新少线表集沿用家族隔离、金额小结的局部掩码和真实空格的严格位置语义。"""
    fourth_path = DEFAULT_GT.with_name("flash_layout_gt_r4_replication_v1.json.gz")
    fourth = ReplicationGold.model_validate_json(primary.read_json(fourth_path))
    prior_paths = (
        primary.DEFAULT_GT,
        DEFAULT_GT,
        DEFAULT_GT.with_name("flash_layout_gt_r3_replication_v1.json.gz"),
    )
    old_sources = {p["pdf_sha256"] for path in prior_paths for p in json.loads(primary.read_json(path))["pages"]}
    assert len(fourth.pages) == len({p.family_id for p in fourth.pages}) == 20
    assert old_sources.isdisjoint(p.pdf_sha256 for p in fourth.pages)
    assert sum(p.stratum == "lightly_ruled" for p in fourth.pages) == 14
    assert sum("table" in p.exhaustive_types for p in fourth.pages) == 19
    tables = [r for page in fourth.pages for r in page.regions if r.type == "table"]
    cells = [c for r in tables for c in r.cell_assertions]
    assert (len(tables), len(cells), sum(c.text == "" for c in cells)) == (24, 139, 4)
    assert sum(len(rel.pairs) for p in fourth.pages for rel in p.relations if rel.kind == "caption_of") == 17
