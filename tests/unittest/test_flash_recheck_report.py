"""验证重跑证据仅由真实三图审阅产生结论，并保护旧问题与源码身份。"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from tools.flash_recheck_report import aggregate, publish_baseline_entry, write_json


@pytest.fixture
def run(tmp_path: Path) -> Path:
    """构造两页、一个旧问题的隔离证据，测试不接触真实 PDF 或历史目录。"""
    root = tmp_path / "flash-recheck"
    old = tmp_path / "flash-pdf-test-20261011"
    root.mkdir()
    source_hash, code, old_code = "a" * 64, "b" * 64, "c" * 64
    document = {"name": "d-test", "path": "/samples/test.pdf", "sha256": source_hash,
                "page_count": 2, "review_pages": [1, 2]}
    write_json(root / "manifest.json", {"documents": [document], "baseline_run": str(old), "code_sha256": code})
    prior = {"id": "prior-1", "document_id": "d-test", "page": 1, "category": "text", "status": "confirmed",
             "source_sha256": source_hash, "code_sha256": old_code, "observed": "旧段落合并", "expected": "保留段界"}
    write_json(root / "prior-findings.json", [prior])
    write_json(root / "queue.json", {"pages": [{"document_id": "d-test", "page": 1, "baseline_status": "issues",
                                               "prior_finding_ids": ["prior-1"]},
                                              {"document_id": "d-test", "page": 2, "baseline_status": "issues",
                                               "prior_finding_ids": []}]})
    state = {"source_sha256": source_hash, "code_sha256": code, "evidence_ready": True,
             "stages": {"parse": "succeeded"}}
    write_json(root / "documents/d-test/status.json", state)
    write_json(old / "documents/d-test/status.json", {**state, "code_sha256": old_code})
    write_json(root / "documents/d-test/page-index.json", {"source_sha256": source_hash, "code_sha256": code,
                                                           "pages": [{"page": page, "blocks": [{"index": 10, "type": "text"}]}
                                                                     for page in (1, 2)]})
    for page in (1, 2):
        for folder, kind in ((root, "source"), (root, "layout"), (old, "layout")):
            with Image.new("RGB", (32, 48), "white") as image:
                image.save(folder / f"documents/d-test/{kind}-{page:04d}.png")
    return root


def review(root: Path, status: str = "fixed", page: int = 1) -> dict:
    """生成显式声明已打开三图并完成旧问题裁决的测试审阅。"""
    decisions = [{"prior_id": "prior-1", "status": status, "note": "实际核查段界", "current_bbox": [0.1, 0.1, 0.9, 0.3],
                  "current_block_indices": [10]}] if page == 1 else []
    value = {"reviewer": "recheck-b", "pages": [{"document_id": "d-test", "page": page, "status": status,
                                                "note": "原页、旧框、新框已核查", "finding_decisions": decisions,
                                                "current_findings": [],
                                                "evidence": [f"documents/d-test/source-{page:04d}.png",
                                                             f"documents/d-test/layout-{page:04d}.png",
                                                             f"../flash-pdf-test-20261011/documents/d-test/layout-{page:04d}.png"]}]}
    write_json(root / "reviews/recheck-b.json", value)
    return value


def current_finding() -> dict:
    """返回带当前块定位及旧问题关联的未修观察。"""
    return {"id": "current-1", "category": "text", "bbox": [0.1, 0.1, 0.9, 0.3], "block_indices": [10],
            "observed": "段落仍合并", "expected": "保留段界", "prior_ids": ["prior-1"], "origin": "retained"}


def test_pending_is_not_inferred_from_success(run: Path) -> None:
    """全部解析成功仍不能将未看图页面或问题标记为修复。"""
    summary = aggregate(run)
    assert summary["parsed_pages"] == 2
    assert summary["reviewed_pages"] == 0
    assert summary["page_status"] == {"pending": 2}
    assert summary["decision_status"] == {"pending": 1}
    assert summary["visual_complete"] is False


def test_complete_requires_all_pages_and_all_prior_decisions(run: Path) -> None:
    """仅审一页不算完成，全部页与旧问题闭环后才算视觉复核完整。"""
    value = review(run)
    assert aggregate(run)["visual_complete"] is False
    second = deepcopy(value["pages"][0])
    second.update(page=2, finding_decisions=[], evidence=[reference.replace("0001", "0002") for reference in second["evidence"]])
    value["pages"].append(second)
    write_json(run / "reviews/recheck-b.json", value)
    assert aggregate(run)["visual_complete"] is True


def test_missing_third_image_rejected(run: Path) -> None:
    """旧布局也是必须实际存在的证据，不能只凭原页与新框裁决。"""
    review(run)
    (run.parent / "flash-pdf-test-20261011/documents/d-test/layout-0001.png").unlink()
    with pytest.raises(ValueError, match="missing evidence"):
        aggregate(run)


def test_missing_declared_old_image_rejected(run: Path) -> None:
    """存在但未声明打开的旧图不能冒充已完成三图审阅。"""
    value = review(run)
    value["pages"][0]["evidence"].pop()
    write_json(run / "reviews/recheck-b.json", value)
    with pytest.raises(ValueError, match="source/old/new"):
        aggregate(run)


def test_duplicate_primary_review_rejected(run: Path) -> None:
    """重复主审不能静默后写覆盖，防止两份不同裁决混合。"""
    value = review(run)
    write_json(run / "reviews/duplicate.json", value)
    with pytest.raises(ValueError, match="Duplicate primary"):
        aggregate(run)


def test_root_adjudication_overrides_and_keeps_history(run: Path) -> None:
    """root 的明确复核可覆盖旧主审，同时保留所有审阅来源。"""
    value = review(run, "unfixed")
    value["reviewer"] = "root"
    value["pages"][0].update(status="fixed", adjudication=True)
    value["pages"][0]["finding_decisions"][0]["status"] = "fixed"
    write_json(run / "reviews/root.json", value)
    assert aggregate(run)["decision_status"] == {"fixed": 1}
    coverage = json.loads((run / "coverage.json").read_text())
    assert len(coverage["pages"][0]["review"]["review_history"]) == 2


def test_cross_page_supplementary_evidence_is_visible(run: Path) -> None:
    """混合旧观察的跨页补充证据必须可直接打开，避免只显示已修复的原锚点页。"""
    value = review(run, "partial")
    value["pages"][0]["evidence"].append("documents/d-test/source-0002.png")
    write_json(run / "reviews/recheck-b.json", value)
    aggregate(run)
    html = (run / "findings.html").read_text()
    assert '补充证据（1）' in html
    assert '<a href="documents/d-test/source-0002.png" target="_blank">' in html
    assert 'width="32" height="48"' in html
    assert "hashchange" in html
    assert "并不自动表示最新版引入的回归" in (run / "REPORT.md").read_text()


def test_resolved_history_includes_partially_improved_pages(run: Path) -> None:
    """旧项已修但页面另有新问题时，默认未修和已修历史两种视图都应能找到该页。"""
    value = review(run)
    value["pages"][0]["status"] = "partial"
    finding = current_finding()
    finding.update(prior_ids=[], origin="newly_observed")
    value["pages"][0]["current_findings"] = [finding]
    write_json(run / "reviews/recheck-b.json", value)
    aggregate(run)
    html = (run / "findings.html").read_text()
    assert 'data-active="true" data-status="partial" data-resolved="true"' in html
    assert "section.dataset.resolved==='true'" in html


def publication_fixture(root: Path) -> bytes:
    """为发布测试补齐完整逐页裁决和旧入口的冻结字节身份。"""
    value = review(root)
    second = deepcopy(value["pages"][0])
    second.update(page=2, finding_decisions=[], evidence=[reference.replace("0001", "0002") for reference in second["evidence"]])
    value["pages"].append(second)
    write_json(root / "reviews/recheck-b.json", value)
    original = b"<!doctype html><title>old evidence</title>"
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["baseline_artifact_sha256"] = {"findings.html": hashlib.sha256(original).hexdigest()}
    write_json(root / "manifest.json", manifest)
    (Path(manifest["baseline_run"]) / "findings.html").write_bytes(original)
    return original


def test_publication_requires_complete_actual_review(run: Path) -> None:
    """仍有未审页时不能替换旧入口或归档，哪怕部分问题已修复。"""
    publication_fixture(run)
    (run / "reviews/recheck-b.json").unlink()
    with pytest.raises(ValueError, match="Complete visual review"):
        publish_baseline_entry(run)
    assert not (run.parent / "flash-pdf-test-20261011/findings-20261011.html").exists()


def test_publication_preserves_exact_old_html_and_is_repeatable(run: Path) -> None:
    """发布及重复运行必须保留逐字节一致的旧报告，同时新报告历史链接指向归档。"""
    original = publication_fixture(run)
    publication = publish_baseline_entry(run)
    archive = Path(publication["archive"])
    assert archive.read_bytes() == original
    assert publication["reviewed_pages"] == 2
    assert "location.replace" in Path(publication["entry"]).read_text()
    assert "findings-20261011.html" in (run / "findings.html").read_text()
    assert publish_baseline_entry(run) == publication
    assert archive.read_bytes() == original


def test_publication_refuses_changed_old_entry(run: Path) -> None:
    """旧入口在冻结后被外部改动时必须拒绝覆盖，避免丢失用户的新改动。"""
    publication_fixture(run)
    entry = run.parent / "flash-pdf-test-20261011/findings.html"
    entry.write_text("user changes")
    with pytest.raises(ValueError, match="changed outside"):
        publish_baseline_entry(run)
    assert entry.read_text() == "user changes"


def test_publication_refuses_conflicting_archive(run: Path) -> None:
    """已有同名归档若不匹配冻结哈希，不能覆盖或继续发布。"""
    original = publication_fixture(run)
    baseline = run.parent / "flash-pdf-test-20261011"
    (baseline / "findings-20261011.html").write_text("unrelated archive")
    with pytest.raises(ValueError, match="archive hash mismatch"):
        publish_baseline_entry(run)
    assert (baseline / "findings.html").read_bytes() == original


@pytest.mark.parametrize("change", ["unknown", "duplicate", "missing", "wrong_page"])
def test_all_old_ids_must_match_current_page(run: Path, change: str) -> None:
    """旧问题编号必须唯一、属于当前物理页，并在审过的页全部闭环。"""
    value = review(run)
    page = value["pages"][0]
    if change == "unknown":
        page["finding_decisions"][0]["prior_id"] = "unknown"
    elif change == "duplicate":
        page["finding_decisions"].append(deepcopy(page["finding_decisions"][0]))
    elif change == "missing":
        page["finding_decisions"] = []
    else:
        page["page"] = 2
        page["evidence"] = [reference.replace("0001", "0002") for reference in page["evidence"]]
    write_json(run / "reviews/recheck-b.json", value)
    with pytest.raises(ValueError, match="prior finding"):
        aggregate(run)


def test_current_block_indices_are_not_old_array_offsets(run: Path) -> None:
    """当前块编号必须来自该页索引，不能拿旧编号或列表偏移冒充。"""
    value = review(run)
    value["pages"][0]["finding_decisions"][0]["current_block_indices"] = [0]
    write_json(run / "reviews/recheck-b.json", value)
    with pytest.raises(ValueError, match="Unknown current block"):
        aggregate(run)


@pytest.mark.parametrize("field", ["source_sha256", "code_sha256"])
def test_frozen_old_identity_must_match(run: Path, field: str) -> None:
    """旧问题的源和源码绑定不能被本轮版本覆盖或任意替换。"""
    prior = json.loads((run / "prior-findings.json").read_text())
    prior[0][field] = "d" * 64
    write_json(run / "prior-findings.json", prior)
    with pytest.raises(ValueError, match="Prior .* hash mismatch"):
        aggregate(run)


def test_current_index_identity_must_match(run: Path) -> None:
    """新页索引必须与当前解析源及源码身份一致。"""
    review(run)
    index = json.loads((run / "documents/d-test/page-index.json").read_text())
    index["code_sha256"] = "d" * 64
    write_json(run / "documents/d-test/page-index.json", index)
    with pytest.raises(ValueError, match="Page index identity"):
        aggregate(run)


def test_fixed_page_cannot_hide_current_findings(run: Path) -> None:
    """有保留问题的页不能宣称全部修复，即使旧记录被写成已修。"""
    value = review(run)
    value["pages"][0]["current_findings"] = [{**current_finding(), "origin": "newly_observed", "prior_ids": []}]
    write_json(run / "reviews/recheck-b.json", value)
    with pytest.raises(ValueError, match="Fixed page contains"):
        aggregate(run)


def test_fixed_page_cannot_hide_unresolved_old_finding(run: Path) -> None:
    """固定页结论必须与每条旧问题一致，不允许仍未修的旧问题漏报。"""
    value = review(run)
    value["pages"][0]["finding_decisions"][0]["status"] = "unfixed"
    write_json(run / "reviews/recheck-b.json", value)
    with pytest.raises(ValueError, match="Fixed page contains"):
        aggregate(run)


def test_retained_finding_keeps_new_location_and_history(run: Path) -> None:
    """未修观察带新块号、三图和旧ID，固定的旧源码仍独立保留。"""
    value = review(run, "partial")
    value["pages"][0]["current_findings"] = [current_finding()]
    write_json(run / "reviews/recheck-b.json", value)
    summary = aggregate(run)
    actual = json.loads((run / "unresolved-findings.json").read_text())[0]
    assert actual["prior_ids"] == ["prior-1"]
    assert actual["current_code_sha256"] == "b" * 64
    assert actual["baseline_code_sha256"] == "c" * 64
    assert actual["block_refs"] == [{"index": 10, "type": "text"}]
    assert summary["decision_status"] == {"partial": 1}


def test_uncertain_without_duplicate_finding_stays_visible(run: Path) -> None:
    """待确认旧问题即使没有重复新建 finding，也不得从默认未修清单消失。"""
    review(run, "uncertain")
    summary = aggregate(run)
    actual = json.loads((run / "unresolved-findings.json").read_text())[0]
    assert actual["status"] == "uncertain"
    assert summary["unresolved_current_findings"] == 1


def test_new_findings_require_current_real_indices(run: Path) -> None:
    """本轮新增观察仍要引用真实当前块，不能绕过旧问题校验。"""
    value = review(run, "unfixed")
    value["pages"][0]["current_findings"] = [{**current_finding(), "origin": "newly_observed", "prior_ids": [], "block_indices": [99]}]
    write_json(run / "reviews/recheck-b.json", value)
    with pytest.raises(ValueError, match="Unknown current block"):
        aggregate(run)


def test_page_only_retained_observation_allowed_without_fabricated_id(run: Path) -> None:
    """旧页只有页级问题时可保留真实观察，不要求伪造不存在的代表问题编号。"""
    value = review(run, "unfixed", page=2)
    value["pages"][0]["current_findings"] = [{**current_finding(), "prior_ids": []}]
    write_json(run / "reviews/recheck-b.json", value)
    assert aggregate(run)["unresolved_current_findings"] == 1


@pytest.mark.parametrize("page,baseline_status", [(1, "issues"), (2, "no_obvious_issue")])
def test_retained_without_ids_is_not_a_general_escape(run: Path, page: int, baseline_status: str) -> None:
    """已有具体旧问题或旧页非问题时，不能用空关联绕过来源约束。"""
    value = review(run, "unfixed", page=page)
    value["pages"][0]["current_findings"] = [{**current_finding(), "prior_ids": []}]
    write_json(run / "reviews/recheck-b.json", value)
    queue = json.loads((run / "queue.json").read_text())
    queue["pages"][page - 1]["baseline_status"] = baseline_status
    write_json(run / "queue.json", queue)
    with pytest.raises(ValueError, match="Retained finding requires"):
        aggregate(run)


def test_html_defaults_to_open_has_three_columns_and_escapes(run: Path) -> None:
    """静态图库默认过滤当前问题，三图可打开全图，非可信文字始终转义。"""
    value = review(run, "unfixed")
    value["pages"][0]["note"] = "<script>alert(1)</script>"
    write_json(run / "reviews/recheck-b.json", value)
    aggregate(run)
    page = (run / "findings.html").read_text()
    assert '<option value="open">当前未修与新增</option>' in page
    assert 'grid-template-columns:repeat(3,minmax(0,1fr))' in page
    assert 'target="_blank"' in page
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in page
    assert '<script>alert(1)</script>' not in page
    assert (run / "index.html").read_text() == (run / "overview.html").read_text()
