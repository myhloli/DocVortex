"""合并逐项真实视觉复核，发布当前未修证据且保留旧轮问题历史。"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import html
import json
import math
import os
from pathlib import Path
import sys
from typing import Any

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.flash_corpus_report import CATEGORIES


STATUSES = {"fixed", "partial", "unfixed", "uncertain", "withdrawn"}
OPEN_STATUSES = {"partial", "unfixed", "uncertain"}
LABELS = {"fixed": "已修复", "partial": "部分修复", "unfixed": "未修复", "uncertain": "待确认",
          "withdrawn": "撤回旧观察", "pending": "未审"}


def read_json(path: Path) -> Any:
    """读取运行证据；其中 PDF 文字和审阅内容只作为数据使用。"""
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    """原子发布可再生汇总，不改写旧轮产物或独立审阅记录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _box(value: Any, location: str) -> list:
    """严格校验原页显示方向的有限归一化区域，拒绝无效定位。"""
    if (not isinstance(value, list) or len(value) != 4
            or any(type(number) not in {int, float} or not math.isfinite(number) for number in value)
            or not 0 <= value[0] < value[2] <= 1 or not 0 <= value[1] < value[3] <= 1):
        raise ValueError(f"Invalid normalized bbox: {location}")
    return value


def required_evidence(root: Path, baseline: Path, document_id: str, page: int) -> list[str]:
    """返回同一物理页原图、旧框和新框三图的相对引用。"""
    return [f"documents/{document_id}/source-{page:04d}.png",
            os.path.relpath(baseline / "documents" / document_id / f"layout-{page:04d}.png", root),
            f"documents/{document_id}/layout-{page:04d}.png"]


def _evidence(root: Path, baseline: Path, references: Any, required: list[str]) -> None:
    """只接受本轮或冻结旧轮的真实文件，并要求三张指定证据均被声明已打开。"""
    if not isinstance(references, list) or not set(required) <= set(references):
        raise ValueError(f"Actual source/old/new evidence required: {required}")
    for reference in references:
        path = (root / reference).resolve()
        if not (path.is_relative_to(root) or path.is_relative_to(baseline)) or not path.is_file():
            raise ValueError(f"Invalid or missing evidence: {reference}")


def _indices(value: Any, blocks: list[dict], location: str) -> tuple[list[int], list[dict]]:
    """按当前页真实编号匹配块，空列表只表示当前没有输出可引用块。"""
    if not isinstance(value, list) or any(type(index) is not int for index in value) or len(set(value)) != len(value):
        raise ValueError(f"Invalid current block indices: {location}")
    available = {block.get("index") for block in blocks}
    if not set(value) <= available:
        raise ValueError(f"Unknown current block indices: {location}: {set(value) - available}")
    return value, [block for block in blocks if block.get("index") in value]


def _identity(root: Path, baseline: Path, document: dict, code: str | None) -> dict:
    """核对当前与旧轮源文件身份，源码身份只取实际运行记录而非推造。"""
    folder = root / "documents" / document["name"]
    state = read_json(folder / "status.json") if (folder / "status.json").is_file() else {}
    old_path = baseline / "documents" / document["name"] / "status.json"
    old = read_json(old_path) if old_path.is_file() else {}
    for label, evidence in (("current", state), ("baseline", old)):
        if evidence and evidence.get("source_sha256") != document["sha256"]:
            raise ValueError(f"{label} source hash mismatch: {document['name']}")
    if state and code and state.get("code_sha256") != code:
        raise ValueError(f"Current code hash mismatch: {document['name']}")
    return {"source_sha256": document["sha256"], "source_path": document["path"],
            "current_code_sha256": state.get("code_sha256", code),
            "baseline_code_sha256": old.get("code_sha256"), "state": state}


def collect_reviews(root: Path, manifest: dict, queue: list[dict], prior: list[dict]) -> tuple[dict, list, list, dict]:
    """校验独立逐页复核及逐问题闭环，普通重复主审直接拒绝，root 可显式裁决。"""
    baseline = Path(manifest["baseline_run"]).resolve()
    documents = {document["name"]: document for document in manifest["documents"]}
    if len(documents) != len(manifest["documents"]):
        raise ValueError("Duplicate manifest document id")
    queued = {(row["document_id"], row["page"]): row for row in queue}
    if len(queued) != len(queue):
        raise ValueError("Duplicate queued physical page")
    old_by_id = {finding["id"]: finding for finding in prior}
    if len(old_by_id) != len(prior):
        raise ValueError("Duplicate prior finding id")
    by_page = defaultdict(set)
    identities = {key: _identity(root, baseline, document, manifest.get("code_sha256"))
                  for key, document in documents.items()}
    for finding in prior:
        key = finding["document_id"], finding["page"]
        if key not in queued or finding["document_id"] not in documents:
            raise ValueError(f"Prior finding missing from queue: {finding['id']}")
        identity = identities[finding["document_id"]]
        if finding.get("source_sha256") != identity["source_sha256"]:
            raise ValueError(f"Prior source hash mismatch: {finding['id']}")
        if identity["baseline_code_sha256"] and finding.get("code_sha256") != identity["baseline_code_sha256"]:
            raise ValueError(f"Prior code hash mismatch: {finding['id']}")
        by_page[key].add(finding["id"])
    for key, row in queued.items():
        if key[0] not in documents or type(key[1]) is not int or not 1 <= key[1] <= documents[key[0]]["page_count"]:
            raise ValueError(f"Invalid queued physical page: {key}")
        if set(row.get("prior_finding_ids", [])) != by_page[key]:
            raise ValueError(f"Queued prior finding ids mismatch: {key}")
    votes = defaultdict(list)
    for record in sorted((root / "reviews").glob("**/*.json")):
        data = read_json(record)
        for row in data.get("pages", []):
            key = row["document_id"], row["page"]
            if key not in queued or row.get("status") not in STATUSES or not row.get("note", "").strip():
                raise ValueError(f"Invalid page review: {record}: {key}")
            _evidence(root, baseline, row.get("evidence"), required_evidence(root, baseline, *key))
            identity = identities[key[0]]
            if not identity["state"].get("evidence_ready"):
                raise ValueError(f"Reviewed document evidence is not ready: {key[0]}")
            index = read_json(root / "documents" / key[0] / "page-index.json")
            if index.get("source_sha256") != identity["source_sha256"] or index.get("code_sha256") != identity["current_code_sha256"]:
                raise ValueError(f"Page index identity mismatch: {key[0]}")
            page = next((page for page in index["pages"] if page["page"] == key[1]), None)
            if page is None:
                raise ValueError(f"Current physical page is missing: {key}")
            decisions = []
            seen = set()
            for decision in row.get("finding_decisions", []):
                prior_id = decision["prior_id"]
                if prior_id not in by_page[key] or prior_id in seen:
                    raise ValueError(f"Unknown, wrong-page or duplicate prior finding: {prior_id}")
                seen.add(prior_id)
                if decision.get("status") not in STATUSES or not decision.get("note", "").strip():
                    raise ValueError(f"Invalid finding decision: {prior_id}")
                _box(decision.get("current_bbox"), prior_id)
                indices, references = _indices(decision.get("current_block_indices"), page["blocks"], prior_id)
                decisions.append({**decision, "document_id": key[0], "page": key[1],
                                  "current_block_indices": indices, "current_block_refs": references,
                                  "prior_finding": old_by_id[prior_id], **{k: v for k, v in identity.items() if k != "state"}})
            if seen != by_page[key]:
                raise ValueError(f"Reviewed page has unclosed prior findings: {key}: {by_page[key] - seen}")
            current = []
            for finding in row.get("current_findings", []):
                if finding.get("category") not in CATEGORIES or finding.get("origin") not in {"retained", "newly_observed"}:
                    raise ValueError(f"Invalid current finding: {finding.get('id')}")
                _box(finding.get("bbox"), finding.get("id", "current"))
                indices, references = _indices(finding.get("block_indices"), page["blocks"], finding["id"])
                linked = finding.get("prior_ids", [])
                if not isinstance(linked, list) or not set(linked) <= by_page[key]:
                    raise ValueError(f"Current finding has wrong-page prior ids: {finding['id']}")
                page_only_observation = (not by_page[key] and queued[key].get("baseline_status") in {"issues", "ambiguous"})
                if finding["origin"] == "retained" and not linked and not page_only_observation:
                    raise ValueError(f"Retained finding requires prior ids: {finding['id']}")
                if any(decision["prior_id"] in linked and decision["status"] not in OPEN_STATUSES for decision in decisions):
                    raise ValueError(f"Current finding contradicts resolved prior finding: {finding['id']}")
                if not finding.get("observed", "").strip() or not finding.get("expected", "").strip():
                    raise ValueError(f"Current finding requires observed and expected: {finding['id']}")
                current.append({**finding, "document_id": key[0], "page": key[1], "block_indices": indices,
                                "block_refs": references, "status": "uncertain" if row["status"] == "uncertain" else "confirmed",
                                "evidence": row["evidence"], **{k: v for k, v in identity.items() if k != "state"}})
            if row["status"] == "fixed" and (current or any(decision["status"] in OPEN_STATUSES for decision in decisions)):
                raise ValueError(f"Fixed page contains unresolved findings: {key}")
            if row["status"] == "withdrawn" and (current or any(decision["status"] != "withdrawn" for decision in decisions)):
                raise ValueError(f"Withdrawn page contains retained findings: {key}")
            votes[key].append({**row, "finding_decisions": decisions, "current_findings": current,
                               "reviewer": data["reviewer"], "record": str(record.relative_to(root))})
    reviews = {}
    decisions = []
    current = []
    ids = set()
    for key, rows in votes.items():
        decisive = [row for row in rows if row.get("adjudication") is True and row["reviewer"] == "root"]
        if len(decisive) > 1 or (not decisive and len(rows) != 1):
            raise ValueError(f"Duplicate primary page reviews: {key}")
        accepted = decisive[0] if decisive else rows[0]
        reviews[key] = {**accepted, "review_history": [{"reviewer": row["reviewer"], "record": row["record"],
                                                      "status": row["status"]} for row in rows]}
        for decision in accepted["finding_decisions"]:
            decisions.append({**decision, "reviewer": accepted["reviewer"], "record": accepted["record"],
                              "evidence": accepted["evidence"]})
        for finding in accepted["current_findings"]:
            if not finding.get("id") or finding["id"] in ids:
                raise ValueError(f"Duplicate current finding id: {finding.get('id')}")
            ids.add(finding["id"])
            current.append({**finding, "reviewer": accepted["reviewer"], "record": accepted["record"]})
    return reviews, decisions, current, identities


def unresolved_findings(decisions: list[dict], current: list[dict]) -> list[dict]:
    """保留实际未闭环观察；无新独立记录的待确认项也必须在默认清单可见。"""
    result = list(current)
    represented = {prior_id for finding in current for prior_id in finding.get("prior_ids", [])}
    for decision in decisions:
        if decision["status"] not in OPEN_STATUSES or decision["prior_id"] in represented:
            continue
        old = decision["prior_finding"]
        result.append({"id": f"retained-{decision['prior_id']}", "document_id": decision["document_id"],
                       "page": decision["page"], "category": old["category"], "origin": "retained",
                       "prior_ids": [decision["prior_id"]], "bbox": decision["current_bbox"],
                       "block_indices": decision["current_block_indices"], "block_refs": decision["current_block_refs"],
                       "observed": decision["note"], "expected": old.get("expected", ""),
                       "status": "uncertain" if decision["status"] == "uncertain" else "confirmed",
                       "evidence": decision["evidence"], "source_sha256": decision["source_sha256"],
                       "source_path": decision["source_path"], "current_code_sha256": decision["current_code_sha256"],
                       "baseline_code_sha256": decision["baseline_code_sha256"], "reviewer": decision["reviewer"],
                       "record": decision["record"]})
    return result


def aggregate(root: Path) -> dict:
    """按冻结问题页队列发布覆盖，不从解析成功、模型差异或旧结论推断修复。"""
    root = root.resolve()
    manifest = read_json(root / "manifest.json")
    queued = read_json(root / "queue.json")["pages"]
    prior = read_json(root / "prior-findings.json")
    reviews, decisions, current, identities = collect_reviews(root, manifest, queued, prior)
    resolved = {decision["prior_id"]: decision for decision in decisions}
    published_decisions = []
    for old in prior:
        published_decisions.append(resolved.get(old["id"], {"prior_id": old["id"], "document_id": old["document_id"],
                                                            "page": old["page"], "status": "pending", "prior_finding": old,
                                                            "note": "尚未实际打开本页三图并完成逐项视觉裁决。"}))
    coverage = [{**row, "status": reviews[(row["document_id"], row["page"])]["status"]
                 if (row["document_id"], row["page"]) in reviews else "pending",
                 "review": reviews.get((row["document_id"], row["page"]))} for row in queued]
    unresolved = unresolved_findings(decisions, current)
    statuses = Counter(row["status"] for row in coverage)
    decision_statuses = Counter(row["status"] for row in published_decisions)
    parsed = [doc for doc in manifest["documents"] if identities[doc["name"]]["state"].get("stages", {}).get("parse") == "succeeded"]
    summary = {"baseline_run": manifest["baseline_run"], "code_sha256": manifest.get("code_sha256"),
               "environment": manifest.get("environment", {}), "documents": len(manifest["documents"]),
               "source_pages": sum(doc["page_count"] for doc in manifest["documents"]),
               "parsed_documents": len(parsed), "parsed_pages": sum(doc["page_count"] for doc in parsed),
               "planned_visual_pages": len(queued), "reviewed_pages": len(reviews),
               "page_status": dict(statuses), "prior_findings": len(prior), "reviewed_prior_findings": len(decisions),
               "decision_status": dict(decision_statuses), "unresolved_current_findings": len(unresolved),
               "newly_observed_findings": sum(row["origin"] == "newly_observed" for row in current),
               "visual_complete": len(reviews) == len(queued) and len(decisions) == len(prior)}
    write_json(root / "review-summary.json", summary)
    write_json(root / "coverage.json", {"summary": summary, "pages": coverage})
    write_json(root / "decisions.json", published_decisions)
    write_json(root / "unresolved-findings.json", unresolved)
    render_html(root, manifest, summary, coverage, published_decisions, unresolved)
    render_report(root, summary)
    return summary


def _escape(value: Any) -> str:
    """统一转义证据中的非可信文本，禁止 PDF 内容成为页面脚本或标记。"""
    return html.escape(str(value), quote=True)


def render_html(root: Path, manifest: dict, summary: dict, coverage: list[dict], decisions: list[dict], findings: list[dict]) -> None:
    """生成三列可点开全图的静态证据页，默认只展示未修、部分修复、待确认及新增。"""
    from PIL import Image

    documents = {doc["name"]: doc for doc in manifest["documents"]}
    by_page = defaultdict(list)
    current = defaultdict(list)
    for decision in decisions:
        by_page[(decision["document_id"], decision["page"])].append(decision)
    for finding in findings:
        current[(finding["document_id"], finding["page"])].append(finding)
    styles = """body{font:14px system-ui;color:#252525;background:#f4f5f5;margin:24px}h1{font-size:24px}
    h1,h2,p,td,li{overflow-wrap:anywhere}h2{font-size:18px}.controls{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0}
    select,input{font:inherit;padding:6px}section{border-top:1px solid #aaa;padding:18px 0}.triple{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
    figure{margin:0}img{display:block;width:100%;height:auto}figcaption{padding:6px 0;font-weight:600}table{border-collapse:collapse;width:100%}
    td,th{text-align:left;border-bottom:1px solid #ccc;padding:8px}details{margin:12px 0}.scroll{overflow:auto}[hidden]{display:none!important}
    @media(max-width:750px){body{margin:16px}.triple{grid-template-columns:1fr}table{min-width:760px}}"""
    sections = []
    for row in coverage:
        key = row["document_id"], row["page"]
        original = by_page[key]
        active = row["status"] in OPEN_STATUSES or bool(current[key])
        resolved_history = row["status"] in {"fixed", "withdrawn"} or any(item["status"] in {"fixed", "withdrawn"} for item in original)
        categories = sorted({item["category"] for item in current[key]} | {item["prior_finding"]["category"] for item in original})
        figures = []
        for label, reference in zip(("原页", "旧布局", "当前布局"), required_evidence(root, Path(manifest["baseline_run"]), *key)):
            if (root / reference).is_file():
                with Image.open(root / reference) as image:
                    width, height = image.size
                visual = (f'<a href="{_escape(reference)}" target="_blank"><img loading="lazy" '
                          f'width="{width}" height="{height}" src="{_escape(reference)}" alt="{label}"></a>')
            else:
                visual = "图像尚未就绪"
            figures.append(f"<figure><figcaption>{label}</figcaption>{visual}</figure>")
        old_items = "".join(f"<li><strong>{_escape(item['prior_id'])} · {LABELS[item['status']]}</strong>："
                            f"{_escape(item['note'])}<br>旧观察：{_escape(item['prior_finding'].get('observed', ''))}"
                            f"<br>期望：{_escape(item['prior_finding'].get('expected', ''))}</li>" for item in original)
        current_items = "".join(f"<li><strong>{_escape(item['id'])} · {_escape(CATEGORIES[item['category']])}</strong>："
                                f"{_escape(item['observed'])}<br>期望：{_escape(item['expected'])}"
                                f"<br>当前块：{_escape(item['block_indices'])}；区域：{_escape(item['bbox'])}；旧ID：{_escape(item.get('prior_ids', []))}</li>"
                                for item in current[key])
        review_note = (row.get("review") or {}).get("note", "尚未实际看图，不能依据差异自动判定是否修复。")
        extra_evidence = [reference for reference in (row.get("review") or {}).get("evidence", [])
                          if reference not in required_evidence(root, Path(manifest["baseline_run"]), *key)]
        extra_links = "".join(f'<li><a href="{_escape(reference)}" target="_blank">{_escape(reference)}</a></li>'
                              for reference in extra_evidence)
        supplementary = (f'<details><summary>补充证据（{len(extra_evidence)}）</summary><ul>{extra_links}</ul></details>'
                         if extra_evidence else "")
        sections.append(f'<section id="{_escape(key[0])}-p{key[1]}" data-active="{str(active).lower()}" '
                        f'data-status="{row["status"]}" data-resolved="{str(resolved_history).lower()}" '
                        f'data-categories="{_escape(" ".join(categories))}">'
                        f'<h2>{_escape(Path(documents[key[0]]["path"]).name)} · 物理页 {key[1]} · {LABELS[row["status"]]}</h2>'
                        f'<p>{_escape(review_note)}</p><div class="triple">{"".join(figures)}</div>{supplementary}'
                        f'<h3>当前保留观察</h3><ul>{current_items or "<li>无已确认保留记录。</li>"}</ul>'
                        f'<details><summary>旧问题逐项裁决（{len(original)}）</summary><ul>{old_items}</ul></details></section>')
    options = "".join(f'<option value="{key}">{_escape(value)}</option>' for key, value in CATEGORIES.items())
    controls = ('<div class="controls"><label>范围 <select id="scope"><option value="open">当前未修与新增</option>'
                '<option value="all">全部历史与未审</option><option value="fixed">含已修复与撤回旧项</option>'
                '<option value="pending">未审</option></select></label><label>类别 <select id="category">'
                f'<option value="">全部类别</option>{options}</select></label><label>查找 <input id="search" type="search"></label></div>')
    script = """<script>
    const scope=document.getElementById('scope'),category=document.getElementById('category'),search=document.getElementById('search');
    // 按明确审阅状态筛选，不由图像差异推断修复。
    function filter(){document.querySelectorAll('section').forEach(section=>{const status=section.dataset.status;
    const range=scope.value==='all'||(scope.value==='open'&&section.dataset.active==='true')||
    (scope.value==='fixed'&&section.dataset.resolved==='true')||(scope.value==='pending'&&status==='pending');
    section.hidden=!(range&&(!category.value||section.dataset.categories.split(' ').includes(category.value))&&
    section.textContent.toLowerCase().includes(search.value.toLowerCase()));});}
    [scope,category,search].forEach(control=>control.addEventListener('input',filter));
    // 图片预留原始比例，深链切换后重新定位到对应物理页。
    function revealTarget(){const target=document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if(target){scope.value='all';filter();requestAnimationFrame(()=>target.scrollIntoView());}else{filter();}}
    window.addEventListener('hashchange',revealTarget);revealTarget();
    </script>"""
    baseline = Path(manifest["baseline_run"])
    history = baseline / "findings-20261011.html"
    history_reference = os.path.relpath(history if history.exists() else baseline / "findings.html", root)
    (root / "findings.html").write_text('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                                        f'<title>Flash 当前问题证据</title><style>{styles}</style><h1>Flash 当前问题证据</h1>'
                                        '<nav><a href="overview.html">复核总览</a> · <a href="REPORT.md">报告</a> · '
                                        '<a href="unresolved-findings.json">当前问题 JSON</a> · <a href="decisions.json">全部裁决 JSON</a> · '
                                        f'<a href="{_escape(history_reference)}">旧轮历史</a></nav>'
                                        f'<p>实际复核 {summary["reviewed_pages"]} / {summary["planned_visual_pages"]} 页；'
                                        f'逐问题裁决 {summary["reviewed_prior_findings"]} / {summary["prior_findings"]}。未审不等于已修复。</p>'
                                        + controls + "".join(sections) + script, encoding="utf-8")
    rows = []
    for document in documents.values():
        selected = [row for row in coverage if row["document_id"] == document["name"]]
        counts = Counter(row["status"] for row in selected)
        first = min((row["page"] for row in selected), default=1)
        rows.append(f'<tr><td><a href="findings.html#{_escape(document["name"])}-p{first}">{_escape(Path(document["path"]).name)}</a></td>'
                    f'<td>{document["page_count"]}</td><td>{len(selected)}</td><td>{len(selected)-counts["pending"]}</td>'
                    f'<td>{counts["fixed"]}</td><td>{counts["partial"]+counts["unfixed"]}</td><td>{counts["uncertain"]}</td><td>{counts["pending"]}</td></tr>')
    overview = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                f'<title>Flash 最新代码复核</title><style>{styles}</style><h1>Flash 最新代码复核</h1>'
                '<nav><a href="findings.html">当前问题证据</a> · <a href="REPORT.md">报告</a> · <a href="coverage.json">逐页记录</a></nav>'
                f'<p>完整解析 {summary["parsed_documents"]} / {summary["documents"]} 份文档，{summary["parsed_pages"]} 页。'
                f'问题页实际视觉复核 {summary["reviewed_pages"]} / {summary["planned_visual_pages"]} 页。'
                f'旧问题逐项裁决 {summary["reviewed_prior_findings"]} / {summary["prior_findings"]} 条。</p>'
                '<div class="scroll"><table><thead><tr><th>文档</th><th>全文页数</th><th>问题页</th><th>已审</th>'
                '<th>已修</th><th>仍有问题</th><th>待确认</th><th>未审</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>')
    (root / "overview.html").write_text(overview, encoding="utf-8")
    (root / "index.html").write_text(overview, encoding="utf-8")


def render_report(root: Path, summary: dict) -> None:
    """明确区分完整解析覆盖、实际看图覆盖、旧观察裁决与新增问题。"""
    pages = "、".join(f"{LABELS[key]} {value} 页" for key, value in summary["page_status"].items())
    decisions = "、".join(f"{LABELS[key]} {value} 条" for key, value in summary["decision_status"].items())
    (root / "REPORT.md").write_text(
        "# Flash 最新代码问题页复核\n\n"
        f"- 完整解析：{summary['parsed_documents']} / {summary['documents']} 份，{summary['parsed_pages']} / {summary['source_pages']} 页。\n"
        f"- 实际视觉覆盖：{summary['reviewed_pages']} / {summary['planned_visual_pages']} 个旧问题页。{pages}。\n"
        f"- 旧问题逐项裁决：{summary['reviewed_prior_findings']} / {summary['prior_findings']} 条。{decisions}。\n"
        f"- 当前保留观察：{summary['unresolved_current_findings']} 条，含本轮新观察 {summary['newly_observed_findings']} 条。\n"
        f"- 视觉复核完整：{'是' if summary['visual_complete'] else '否'}。\n\n"
        "本轮只更新证据，不修改解析算法或公共 API。每条旧问题依原页、旧布局、当前布局逐项裁决；"
        "未审页面及问题保持 pending，解析成功、块号变化和自动差异不能证明修复。"
        "原生能力边界不自动提升为缺陷，旧观察撤回保留说明。问题观察条数不等同独立缺陷数量。\n\n"
        "本轮新观察表示旧记录未单列、此次实际看图确认的问题，并不自动表示最新版引入的回归。"
        f"完整解析覆盖全文；本轮逐页视觉覆盖严格限于冻结的旧问题/待确认页，不声称全文{summary['source_pages']}页均已视觉通过。\n\n"
        "[当前问题证据](findings.html) · [文档总览](overview.html) · [逐页记录](coverage.json) · "
        "[全部裁决](decisions.json) · [当前保留问题](unresolved-findings.json)\n\n"
        f"旧轮证据保留在 `{summary['baseline_run']}`。当前源码指纹：`{summary.get('code_sha256')}`。\n",
        encoding="utf-8")


def publish_baseline_entry(root: Path) -> dict:
    """仅在完整复核后归档字节一致的旧入口，并发布指向当前问题页的可重入入口。"""
    root = root.resolve()
    summary = aggregate(root)
    if not summary["visual_complete"] or summary["parsed_documents"] != summary["documents"]:
        raise ValueError("Complete visual review and parsing required before replacing baseline entry")
    manifest = read_json(root / "manifest.json")
    baseline = Path(manifest["baseline_run"])
    entry, archive = baseline / "findings.html", baseline / "findings-20261011.html"
    expected = manifest["baseline_artifact_sha256"]["findings.html"]
    original = entry.read_bytes()
    current_hash = hashlib.sha256(original).hexdigest()
    if archive.exists() and hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise ValueError("Baseline archive hash mismatch; existing archive must not be overwritten")
    if current_hash != expected:
        publication_path = root / "publication.json"
        previous = read_json(publication_path) if publication_path.exists() else {}
        if (not archive.exists() or previous.get("entry_sha256") != current_hash
                or previous.get("baseline_html_sha256") != expected):
            raise ValueError("Baseline entry changed outside this publication; refusing to overwrite")
    if not archive.exists():
        archive.write_bytes(original)
    destination = os.path.relpath(root / "findings.html", baseline)
    escaped = _escape(destination)
    redirect = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                '<title>Flash 最新问题证据</title>'
                f'<meta http-equiv="refresh" content="0;url={escaped}">'
                f'<script>location.replace({json.dumps(destination)}+location.search+location.hash);</script>'
                f'<p><a href="{escaped}">打开最新问题证据</a> · '
                '<a href="findings-20261011.html">查看 2026-10-11 原始历史</a></p>')
    entry.write_text(redirect, encoding="utf-8")
    publication = {"baseline_html_sha256": expected, "archive": str(archive), "entry": str(entry),
                   "entry_sha256": hashlib.sha256(entry.read_bytes()).hexdigest(), "destination": str(root / "findings.html"),
                   "reviewed_pages": summary["reviewed_pages"], "reviewed_prior_findings": summary["reviewed_prior_findings"],
                   "code_sha256": summary["code_sha256"]}
    write_json(root / "publication.json", publication)
    aggregate(root)
    return publication


def main() -> None:
    """只读取指定新运行目录，合并明确审阅记录并输出静态证据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    result = publish_baseline_entry(args.output) if args.publish else aggregate(args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
