"""覆盖可移植 GT 的稀疏计分、关系、类型投影与家族总分。"""

import json

import pytest

from tools.score_flash_layout_gt import aggregate, matches_table_dimensions, read_replay, score_page


def test_sparse_and_exhaustive_inventory_use_distinct_denominators():
    """局部框只评分已标区域；完成整类清点后才扣多出的图，视觉主体与 body 不重复计分。"""
    pytest.importorskip("scipy.optimize")
    box = [0, 0, 0.6, 0.5]
    label = [0, 0.55, 0.6, 0.6]
    extra = [0.65, 0.2, 0.9, 0.3]
    gold = {
        "regions": [
            {"id": "figure", "bbox": box, "type": "image", "scored_fields": ["bbox", "type"]},
            {"id": "caption", "bbox": label, "type": "image_caption", "scored_fields": ["bbox", "type"]},
        ],
        "relations": [
            {"kind": "caption_of", "pairs": [["caption", "figure"]]},
            {"kind": "before", "pairs": [["figure", "caption"]]},
        ],
    }
    page = {
        "blocks": [
            {
                "type": "chart",
                "index": 2,
                "bbox": box,
                "content": [
                    {"type": "chart_body", "index": 2, "bbox": box, "content": ""},
                    {"type": "chart_caption", "index": 3, "bbox": label, "content": "trend"},
                ],
            },
            {"type": "image", "index": 4, "bbox": extra},
        ]
    }
    score = score_page(gold, page, {"caption": ["trend"]})
    assert score["score"] == 1 and score["predicted"] == 3
    assert score["ownership_relations_scored"] == 1 and score["order_count"] == 1
    assert score["exhaustive_inventory"] == []
    audited = score_page({**gold, "exhaustive_types": ["image"]}, page, {"caption": ["trend"]})
    assert audited["exhaustive_inventory"] == [pytest.approx(2 / 3)]


def test_masked_type_and_duplicate_text_are_scored_independently():
    """不确定的类型不扣分，重复输出同一源文字仍然扣内容分。"""
    pytest.importorskip("scipy.optimize")
    box = [0.0, 0.0, 1.0, 0.2]
    gt = {"regions": [{"id": "a", "type": None, "bbox": box, "scored_fields": ["bbox", "content_anchors"]}]}
    score = score_page(gt, {"blocks": [{"type": "text", "bbox": box, "content": "sample sample"}]}, {"a": ["sample"]})
    assert score["type_count"] == 0 and score["dimensions"]["structure_geometry"] == 1
    assert score["dimensions"]["content"] == 0


def test_family_aggregation_gives_equal_weight_to_documents_then_families():
    """同家族内先计算文档均值，避免一个文档的多个页面取得多个家族票。"""
    records = []
    for family, doc, old, new in [("f1", "d1", 0, 1), ("f1", "d1", 1, 1), ("f1", "d2", 0, 0), ("f2", "d3", 1, 1)]:
        records.append(
            {
                "split": "test",
                "cohort": "historical_badcase",
                "family_id": family,
                "document_id": doc,
                "A": {"score": old, "dimensions": {}},
                "D": {"score": new, "dimensions": {}},
            }
        )
    score = aggregate(records)["test"]["score"]["historical_combined"]
    assert (score["families"], score["A"], score["D"], score["delta"]) == (2, 62.5, 75, 12.5)


def test_invalid_table_span_or_mismatched_source_does_not_pass(tmp_path):
    """无效单元格跨度应使结构项失败，含其他源指纹的重放不得计分。"""
    assert matches_table_dimensions("<table><tr><th colspan='2'>标题</th></tr></table>", {"rows": 1, "columns": 2})
    assert not matches_table_dimensions("<table><tr><td colspan='x'>数据</td></tr></table>", {"rows": 1, "columns": 1})
    folder = tmp_path / "documents" / "example"
    folder.mkdir(parents=True)
    (folder / "middle.json").write_text('{"pages":[{}]}')
    (folder / "status.json").write_text(json.dumps({"status": "complete", "sha256": "other"}))
    with pytest.raises(ValueError, match="源指纹"):
        read_replay([tmp_path], {"document_id": "example", "pdf_sha256": "original", "page_count": 1})


def test_type_only_region_uses_its_coarse_location_without_scoring_geometry():
    """粗定位只用于识别对应候选；类型和锚点均能计分，不因粗框惩罚几何。"""
    pytest.importorskip("scipy.optimize")
    region = {"id": "a", "type": "text", "bbox": [0.0, 0.0, 1.0, 0.4], "scored_fields": ["type", "content_anchors"]}
    predicted = {"type": "text", "bbox": [0.0, 0.0, 1.0, 0.12], "content": "sample"}
    scores = score_page({"regions": [region]}, {"blocks": [predicted]}, {"a": ["sample"]})
    assert scores["bbox_count"] == 0
    assert scores["score"] == scores["dimensions"]["content"] == 1
    assert scores["matched"] == scores["type_count"] == scores["owners_count"] == 1

    exact = {"id": "b", "type": None, "bbox": predicted["bbox"], "scored_fields": ["bbox"]}
    conflicts = score_page({"regions": [region, exact]}, {"blocks": [predicted]}, {"a": ["sample"]})
    assert conflicts["matched"] == conflicts["bbox_count"] == 1
    assert conflicts["dimensions"]["ownership"] == 0
    assert conflicts["dimensions"]["structure_geometry"] == 0.5


@pytest.mark.parametrize(("confirmed", "expected"), [(True, 0.0), (False, 1.0)])
def test_only_confirmed_blank_page_penalizes_extra_text_with_table_only_inventory(confirmed, expected):
    """只有确认全空时惩罚清点类型以外的额外内容，普通零表页仍可有正文。"""
    gt = {"regions": [], "blank_verified": confirmed, "exhaustive_types": ["table"]}
    hallucinated = {"type": "text", "bbox": [0.0, 0.2, 0.5, 0.8], "content": "source is blank"}
    assert score_page(gt, {"blocks": [hallucinated]}, {})["score"] == expected
    assert score_page(gt, {}, {})["score"] == 1.0
