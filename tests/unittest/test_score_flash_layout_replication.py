"""独立表格布局评测的边界、局部标注掩码与逻辑单元格回归。"""

import pytest

from tools.score_flash_layout_replication import inspect_table, score_page, table_matches


def test_bottom_relative_cells_match_the_actual_final_row():
    """要求末行值出现在真实表尾，超出网格的倒数行不能命中任意同名单元格。"""
    html = "<table><tr><td>7</td></tr><tr><td>99</td></tr></table>"
    assertions = [
        {"row_from_end": end, "column_0based": 0, "text": text, "rowspan": 1, "colspan": 1}
        for end, text in [(1, "99"), (1, "7"), (2, "7"), (3, "7")]
    ]
    target = {"cell_assertions": assertions}
    assert [r["span_pass"] for r in inspect_table(html, target)["cells"]] == [True, False, True, False]
    assert not any(r["pass"] for r in inspect_table("", target)["cells"])


def test_replication_cell_assertions_preserve_signs_and_spans():
    """跨行跨列按真实逻辑格选择，负号、百分号、小数点及完整单元格不能忽略。"""
    html = "<table><tr><th rowspan='2'>Code</th><th colspan='2'>Values</th></tr><tr><td>−4.15%</td><td>40 mm</td></tr></table>"
    assertions = [
        (1, 0, "Code"),
        (0, 2, "Values"),
        (1, 1, "-4.15%"),
        (1, 1, "4.15%"),
        (1, 1, "-415%"),
        (None, 2, "40mm"),
        (None, 2, "40"),
        (None, 0, "Values"),
    ]
    target = {
        "table_dimensions": {"columns": 3},
        "cell_assertions": [{"row_0based": r, "column_0based": c, "text": t} for r, c, t in assertions],
    }
    result = inspect_table(html, target)
    assert result["dimensions"] == {"columns": {"actual": 3, "expected": 3, "pass": True}}
    assert [r["pass"] for r in result["cells"]] == [True, True, True, False, False, True, False, False]


def test_span_score_requires_the_anchor_text_and_both_extents():
    """表头文字正确仍可能跨错列，续位也不能冒充另一个同名表头的起点。"""
    html = "<table><tr><th rowspan='2'>A</th><th colspan='2'>Group</th></tr><tr><td>−0.8</td><td></td></tr></table>"
    specs = [
        (0, 0, "A", 2, 1),
        (0, 1, "Group", 1, 2),
        (0, 1, "Group", 1, 1),
        (0, 2, "Group", 1, 2),
        (1, 1, "0.8", 1, 1),
        (1, 2, "", 1, 1),
    ]
    cells = [{"row_0based": r, "column_0based": c, "text": t, "rowspan": h, "colspan": w} for r, c, t, h, w in specs]
    target = {"cell_assertions": cells}
    scores = inspect_table(html, target)["cells"]
    assert [c["pass"] for c in scores] == [True, True, True, True, False, True]
    assert [c["span_pass"] for c in scores] == [True, True, False, False, False, True]
    assert scores[3]["actual_span"] is None
    assert not any(c["span_pass"] for c in inspect_table("", target)["cells"])


def test_alternative_boxes_share_one_prediction():
    """等价表框只能获得一次命中，不能为两个 GT 重复使用唯一预测。"""
    pytest.importorskip("scipy.optimize")
    small = [0.1, 0.2, 0.6, 0.4]
    wider = [0.1, 0.2, 0.6, 0.8]
    competing = [0.1, 0.2, 0.6, 0.81]
    scores = table_matches(
        [
            {"id": "a", "bbox": small, "accepted_alternative_bboxes": [wider]},
            {"id": "b", "bbox": competing},
        ],
        [{"bbox": wider}],
    )
    assert scores == {"a": (0, 1.0)}


def test_same_row_assertions_need_distinct_cells_in_one_row():
    """跨行表头可以提供行标，散落在不同行或同一个跨列格的值不能凑成一条记录。"""
    html = "<table><tr><td rowspan='2'>S</td><td>2</td><td>4</td></tr><tr><td>3</td><td></td></tr><tr><td colspan='3'>X</td></tr></table>"
    groups = [
        [(0, "S"), (1, "3"), (2, "")],
        [(0, "S"), (1, "2"), (2, "")],
        [(0, "X"), (1, "X")],
    ]
    target = {"row_assertions": [[{"column_0based": col, "text": val} for col, val in row] for row in groups]}
    assert [r["matching_rows"] for r in inspect_table(html, target)["rows"]] == [[1], [], []]
    assert not any(r["pass"] for r in inspect_table("", target)["rows"])


def test_sparse_page_only_penalizes_declared_protections():
    """局部标注不处罚页内任意附加表，但巨表覆盖独立区或整组时仍会被检出。"""
    zones = [
        {"id": k, "bbox": box, "type": "text", "scored_fields": ["bbox", "type"]}
        for k, box in (
            ("x", [0.25, 0.25, 0.4, 0.4]),
            ("y", [0.5, 0.5, 0.6, 0.6]),
        )
    ]
    gt = {
        "regions": zones,
        "protected_non_tables": ["x", "y"],
        "forbidden_table_combinations": [{"covered_regions": ["x", "y"], "min_coverage_per_region": 0.75}],
        "relations": [],
        "exhaustive_types": [],
    }
    outside = {"type": "table", "index": 0, "bbox": [0, 0, 0.1, 0.1], "content": []}
    enclosing = {**outside, "bbox": [0, 0, 1, 1]}
    good = score_page(gt, {"blocks": [outside]})
    bad = score_page(gt, {"blocks": [enclosing]})
    assert good["extra_tables"] is None and good["metrics"]["exhaustive_page_inventory"] == []
    assert good["protected_non_tables"] == {"x": True, "y": True}
    assert good["forbidden_combinations_pass"] == [True]
    assert bad["extra_tables"] is None and bad["protected_non_tables"] == {"x": False, "y": False}
    assert bad["forbidden_combinations_pass"] == [False]


def test_narrow_grid_challenge_excludes_symbol_images_from_table_hits():
    """同一真表不能重复计数，图像保留不应被算成结构表，只有实际格子可获可编辑分。"""
    pytest.importorskip("scipy.optimize")
    from tools.score_flash_narrow_grid import score_regions

    left = [0.1, 0.1, 0.5, 0.5]
    right = [0.6, 0.1, 0.9, 0.5]
    regions = [{"id": i, "bbox": box} for i, box in enumerate([left, left, right])]
    html = "<table><tr><td>Count</td></tr></table>"
    blocks = [
        {"type": "table", "bbox": left, "content": [{"type": "table_body", "content": html}]},
        {"type": "image", "bbox": right, "content": [{"type": "image_body", "content": "images/1.jpg"}]},
    ]
    hits = score_regions(regions, {"blocks": blocks})
    assert sum(hit["editable"] for hit in hits) == 1
    assert hits[2]["image_iou"] == pytest.approx(1)
    blocks[0]["content"][0]["content"] = "<table></table>"
    assert not any(hit["editable"] for hit in score_regions(regions, {"blocks": blocks}))


def test_frozen_narrow_grid_source_regions():
    """保证局部挑战没有重复原件或外部路径，每个局部有可回查的来源、类别及有效几何。"""
    import json
    from collections import Counter

    from tools.score_flash_narrow_grid import DEFAULT_GT, read_json

    data = json.loads(read_json(DEFAULT_GT))
    pages = data["pages"]
    assert len(pages) == len({p["pdf_sha256"] for p in pages}) == 45
    counts = Counter()
    for page in pages:
        assert 0 < page["physical_page"] <= page["page_count"]
        assert page["document_id"] == "d-" + page["pdf_sha256"][:16]
        assert page["source_bytes"] > 0 and "pdf_path" not in page
        for region in page["regions"]:
            counts[page["cohort"], region["kind"]] += 1
            x0, y0, x1, y1 = region["bbox"]
            assert region["key"] and 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1
    assert counts["narrow-independent", "required_table"] == 9
    assert counts["narrow-confirmation", "required_table"] == 4
    assert sum(counts.values()) == 51
