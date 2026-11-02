"""验证复核范围来自旧问题页，不能因缺少代表问题或新版差异而遗漏。"""

import pytest

from tools.recheck_flash_corpus import partition, review_sheet, select_queue, write_json


def test_queue_includes_pages_without_representative_findings():
    """旧逐页问题观察即使没有finding也必须复核，能力边界不能自动纳入。"""
    manifest = {"documents": [{"name": "d-a", "page_count": 4}]}
    coverage = {"pages": [
        {"document_id": "d-a", "page": 1, "status": "issues"},
        {"document_id": "d-a", "page": 2, "status": "issues"},
        {"document_id": "d-a", "page": 3, "status": "ambiguous"},
        {"document_id": "d-a", "page": 4, "status": "out_of_scope"},
    ]}
    findings = [{"id": "f1", "document_id": "d-a", "page": 1, "status": "confirmed"}]
    documents, pages, prior = select_queue(manifest, coverage, findings)
    assert documents[0]["review_pages"] == [1, 2, 3]
    assert pages[1]["prior_finding_ids"] == []
    assert pages[2]["baseline_status"] == "ambiguous"
    assert prior == findings


def test_orphan_finding_is_not_silently_dropped():
    """旧问题和页级状态不一致时必须停止，不能缩小交付范围。"""
    manifest = {"documents": [{"name": "d-a", "page_count": 1}]}
    finding = {"id": "orphan", "document_id": "d-a", "page": 1, "status": "confirmed"}
    with pytest.raises(ValueError, match="没有对应"):
        select_queue(manifest, {"pages": []}, [finding])


def test_partition_has_no_missing_or_repeated_physical_pages():
    """整本文档分配后，各主审页集合必须互斥并保持完整。"""
    documents = [{"name": f"d-{i}", "review_pages": list(range(1, count + 1))}
                 for i, count in enumerate((20, 15, 10, 8, 6, 3, 2))]
    pages = [{"document_id": document["name"], "page": page}
             for document in documents for page in document["review_pages"]]
    assignments = partition(documents, pages)
    assigned = [(row["document_id"], row["page"])
                for assignment in assignments.values() for row in assignment["pages"]]
    assert len(assigned) == len(set(assigned)) == len(pages)
    assert sum(assignment["page_count"] for assignment in assignments.values()) == len(pages)
    assert max(assignment["page_count"] for assignment in assignments.values()) <= 20


@pytest.fixture
def sheet_run(tmp_path):
    """构造三张左右双色图，验证局部放大始终取自对应原始证据。"""
    from PIL import Image, ImageDraw

    root, old = tmp_path / "new", tmp_path / "old"
    write_json(root / "manifest.json", {"baseline_run": str(old),
                                       "documents": [{"name": "d-test", "review_pages": [1]}]})
    for folder, kind, color in ((root, "source", "blue"), (old, "layout", "green"), (root, "layout", "yellow")):
        target = folder / "documents/d-test" / f"{kind}-0001.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        with Image.new("RGB", (200, 100), "red") as image:
            ImageDraw.Draw(image).rectangle((100, 0, 199, 99), fill=color)
            image.save(target)
    return root


def test_sheet_region_preserves_source_old_current_order(sheet_run):
    """局部放大三列不混用证据，文件名区分整页与裁切且比例正确。"""
    from PIL import Image

    whole = review_sheet(sheet_run, "d-test", [1], width=600)
    cropped = review_sheet(sheet_run, "d-test", [1], width=600, region=[0.5, 0, 1, 1])
    assert whole != cropped
    with Image.open(whole) as image:
        assert image.size == (1800, 340)
    with Image.open(cropped) as image:
        assert image.size == (1800, 640)
        assert image.getpixel((300, 340))[2] > 240
        assert image.getpixel((900, 340))[1] > 110
        red, green, blue = image.getpixel((1500, 340))
        assert red > 240 and green > 240 and blue < 15


@pytest.mark.parametrize("region", [[-0.1, 0, 1, 1], [0.5, 0, 0.5, 1], [0, 1, 1, 0], [0, 0, 2, 1]])
def test_sheet_rejects_invalid_normalized_region(sheet_run, region):
    """无效局部区域必须拒绝，不能静默裁出错误位置并当作视觉证据。"""
    with pytest.raises(ValueError, match="局部放大区域"):
        review_sheet(sheet_run, "d-test", [1], width=600, region=region)


def test_sheet_does_not_expand_frozen_page_scope(sheet_run):
    """不能给未纳入本轮的问题页生成看似已复核的辅助拼图。"""
    with pytest.raises(ValueError, match="冻结"):
        review_sheet(sheet_run, "d-test", [2], width=600)
