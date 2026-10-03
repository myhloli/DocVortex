"""关联候选区域、逐项视觉裁决与修复计划；自动匹配只定位，绝不自动宣布修复。"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

from review_flash_merged import flatten, render_gallery


GROUPS = [
    (
        "脚注识别和分项",
        r"page_footnote|脚注|底部编号",
        "auxiliary_text.py / text_assembly/footnotes.py",
        "分隔线、字号、编号、缩进和净空联合判定；按编号分项并防止正文和页脚混入。",
    ),
    (
        "公式成员与边界",
        r"equation|公式|分式|上下标|不等式|等式",
        "formulas.py",
        "先建立正文与表格屏障，再聚合二维数学成员和唯一编号；空字符矢量公式只保留完整裁图。",
    ),
    (
        "表格范围和成员",
        r"table|表头|表格|单元格|合计行|续表",
        "table_detection.py / table_materialization.py",
        "共享列、分组行和独立表题限定完整表体；表头和合计行唯一归属，禁止包含图体或下方正文。",
    ),
    (
        "图形和图片归属",
        r"image_body|图片|图框|漏.*图|柱|环形|环图|图体|图形|整页.*image|噪声|浅灰|图间缝",
        "graphics.py / native_objects.py / text_noise.py",
        "由图体、坐标和数据标签确认整体图形；保留内容图，过滤背景，避免越界与重复认领。大区域不能扩张后认领无关小框；孤立浅灰数字同时用嵌套或图间缝几何及原生填充颜色判定噪声，正常表格数字、刻度和上下标必须保留。",
    ),
    (
        "标题、段落及阅读顺序",
        r"paragraph_title|doc_title|标题|题名|作者|段|caption|图注|表题|参考文献|文献|阅读|顺序|条目|list|引语|text",
        "title_analysis / text_assembly / postprocess/visual.py",
        "标题、正文、条目和续行按源几何区分；顺序沿实际栏序，图表注释绑定完整且正确的父对象。",
    ),
    (
        "目录及样式",
        r"index|目录|粗体|样式|Contents",
        "index_blocks.py / inline/glyph_weight.py",
        "完整保留目录条目及页码，独立页码移出目录；缺失字重只由同字符、同尺度字形证据补充。",
    ),
]


def categories(case):
    """复合问题可以属于多批，所有要求裁决后才允许闭环。"""
    text = case["expected"]
    result = [name for name, pattern, _, _ in GROUPS if re.search(pattern, text, re.I)]
    return result or ["标题、段落及阅读顺序"]


def overlap(first, second):
    """使用相对交叠定位变化后的成员，不依赖旧块号或全局输出编号。"""
    area = max(0, min(first[2], second[2]) - max(first[0], second[0])) * max(
        0, min(first[3], second[3]) - max(first[1], second[1])
    )
    denominator = min((first[2] - first[0]) * (first[3] - first[1]), (second[2] - second[0]) * (second[3] - second[1]))
    return area / max(denominator, 1e-8)


def anchor_matches(anchor, block):
    """区域与文字分别给出候选定位证据；漏框的矢量公式可只靠已视觉冻结的区域定位。"""
    geographic = overlap(anchor["bbox"], block["bbox"])
    normalized = re.sub(r"\s+", "", block.get("text", ""))
    tokens = [re.sub(r"\s+", "", anchor.get(key, "")) for key in ("text_start", "text_end")]
    textual = any(len(token) >= 8 and (token[:40] in normalized or token[-40:] in normalized) for token in tokens)
    return geographic >= 0.6 or textual and geographic >= 0.1


def record_deduplication(data):
    """用源指纹、物理页、区域和具体问题去重；仅类型相同但要求不同的复合问题继续独立裁决。"""
    groups = {}
    for doc in data["documents"]:
        for case in doc["cases"]:
            regions = sorted(tuple(round(value, 4) for value in anchor["bbox"]) for anchor in case["anchors"])
            key = [doc["sha256"], case["page"], categories(case), regions,
                   re.sub(r"\s+", "", case["expected"]).casefold()]
            digest = hashlib.sha256(json.dumps(key, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            case["deduplication_key"] = digest
            groups.setdefault(digest, []).append(case)
    for members in groups.values():
        for case in members:
            case["duplicate_case_ids"] = [other["id"] for other in members if other is not case]
    duplicate_groups = [[case["id"] for case in members] for members in groups.values() if len(members) > 1]
    data["deduplication"] = {
        "method": "源SHA256、物理页、区域、问题类型及具体要求；同区域不同要求保留独立记录",
        "duplicate_groups": duplicate_groups,
        "unique_requirements": len(groups),
    }


def finalize(manifest, replay, decisions):
    """更新正式可编辑交付物，只有明确的逐项裁决能改变必修问题的修复状态。"""
    if decisions is not None and not decisions.is_file():
        # 显式裁决文件不存在时停止，不能把已确认的验收结果静默重置为待修复。
        raise FileNotFoundError(f"逐项裁决文件不存在：{decisions}")
    data = json.loads(manifest.read_text())
    run = json.loads((replay / "run.json").read_text())
    by_document = {document["name"]: document for document in run["documents"]}
    assert set(by_document) == {document["name"] for document in data["documents"]}
    assert len(run["documents"]) == data["document_count"] == 187
    assert sum(document["pages"] for document in run["documents"]) == data["page_count"] == 208
    verdicts = json.loads(decisions.read_text()) if decisions and decisions.is_file() else {"cases": {}, "pages": {}}
    known_ids = {case["id"] for doc in data["documents"] for case in doc["cases"]}
    assert set(verdicts.get("cases", {})) <= known_ids
    for doc in data["documents"]:
        current = by_document.get(doc["name"])
        if not current:
            continue
        assert current["sha256"] == doc["sha256"]
        after = json.loads((replay / doc["name"] / "middle.json").read_text())
        for page in doc["pages"]:
            number = page["page"]
            page["candidate_image"] = str((replay / doc["name"] / f"layout-{number:03d}.png").relative_to(manifest.parent))
            page["changed"] = number in current["changed_pages"]
            page["visual_decision"] = verdicts.get("pages", {}).get(
                f"{doc['name']}:p{number}", "pending" if page["changed"] else "unchanged"
            )
        for case in doc["cases"]:
            blocks = flatten(after["pages"][case["page"] - 1]["blocks"])
            matched = [block for block in blocks if any(anchor_matches(anchor, block) for anchor in case["anchors"])]
            case["current_indices"] = sorted({block["index"] for block in matched})
            case["current_blocks"] = matched
            case["candidate_code_sha256"] = run["code_sha256"]
            case["repair_batches"] = categories(case)
            case["anchor_mapping_method"] = "原页区域交叠与文字锚点联合匹配，仅用于定位；不构成自动裁决"
            verdict = verdicts.get("cases", {}).get(case["id"])
            if verdict:
                assert case["status"] == "确认问题", case["id"]
                assert verdict["status"] in {"verified", "partial", "pending"}
                if verdict["status"] == "verified":
                    assert verdict.get("tests") and verdict.get("visual_evidence") and verdict.get("reason")
                    assert all((manifest.parent / path).is_file() for path in verdict["visual_evidence"]), case["id"]
                    assert verdict.get("test_log") and (manifest.parent / verdict["test_log"]).is_file(), case["id"]
                case["repair_status"] = verdict["status"]
                case["repair_verification"] = verdict
            elif case["status"] == "确认问题" and case.get("repair_status") != "verified":
                # 本轮只提交新增裁决；已通过基线和完整重放核验的历史结论及证据继续保留。
                case["repair_status"] = "pending"
    data["summary"] = dict(Counter(case["status"] for doc in data["documents"] for case in doc["cases"]))
    data["repair_summary"] = dict(
        Counter(case["repair_status"] for doc in data["documents"] for case in doc["cases"] if case["status"] == "确认问题")
    )
    record_deduplication(data)
    data["candidate_run"] = {
        "path": str(replay),
        "code_sha256": run["code_sha256"],
        "documents": len(run["documents"]),
        "pages": sum(doc["pages"] for doc in run["documents"]),
        "public_check_errors": sum(len(doc.get("public_checks", {}).get("errors", [])) for doc in run["documents"]),
    }
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    render_gallery(manifest.parent, data)
    text = []
    plan = [
        "# Flash 视觉复核与修复执行记录",
        "",
        f"187份原件、208页；来源仅为原页与layout视觉对照。确认问题的修复状态：{data['repair_summary']}。",
        "",
        "单页边缘独立text可接受；不引入OCR。全部确认问题仍属必修，pending/partial表示尚未闭环，不能以测试通过或候选定位替代视觉裁决。",
        "",
        f"候选输出：[{replay.name}](./{replay.name}/comparison.html)；代码指纹 `{run['code_sha256']}`。",
        "",
    ]
    for name, _, implementation, boundary in GROUPS:
        plan.extend(
            [
                f"## {name}",
                "",
                f"实现入口：`{implementation}`。{boundary}",
                "",
                "验收：先冻结原页失败断言，补充混淆反例并改变位置、字号、栏宽；通过针对性检查后重放208页与当前19份168页维护清单，查看每个变化页原图及前后布局。",
                "",
                "|记录|物理页|修复状态|期望与证据|",
                "|---|---|---|---|",
            ]
        )
        for doc in data["documents"]:
            for case in doc["cases"]:
                if case["status"] != "确认问题" or name not in case["repair_batches"]:
                    continue
                expected = case["expected"].replace("|", "／").replace("\n", " ")
                page = doc["pages"][case["page"] - 1]
                plan.append(
                    f"|{case['id']}|{case['page']}|{case['repair_status']}|{expected} [原页]({page['source_image']}) [基线]({page['layout_image']}) [候选]({page.get('candidate_image', '')})|"
                )
        plan.append("")
    for doc in data["documents"]:
        text.extend([doc["name"], f"源文件 {doc['path']} SHA256 {doc['sha256']}"])
        for case in doc["cases"]:
            text.append(
                f"页{case['page']} 原块{','.join(map(str, case['original_indices'])) or '-'} 当前块{','.join(map(str, case['current_indices'])) or '-'} [{case['status']} / {case['repair_status']}] {case['expected']} ({case['id']})"
            )
            if case.get("user_review"):
                text.append(f"  用户批注：{case['user_review']}")
            if case.get("reason"):
                text.append(f"  裁决依据：{case['reason']}")
        text.append("")
    (manifest.parent / "mark.reviewed.txt").write_text("\n".join(text) + "\n")
    (manifest.parent / "repair-plan.md").write_text("\n".join(plan) + "\n")
    print(json.dumps(data["repair_summary"], ensure_ascii=False))


def main():
    """接受显式合并清单、候选重放和人工式逐项裁决文件，不扫描benchmark。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--decisions", type=Path)
    args = parser.parse_args()
    finalize(args.manifest, args.replay, args.decisions)


if __name__ == "__main__":
    main()
