"""合并本轮人工与视觉标注并重放明确的 PDF 白名单，禁止读取 benchmark GT。"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import shutil
from copy import deepcopy


MANUAL = {
    1: [("0", "边缘文字误为 equation，应为独立 text"), ("4", "末段正文与脚注18分开")],
    2: [("0", "边缘文字误为 equation，应为独立 text"), ("2", "章节8标题与正文分开"), ("4", "脚注19从末段切出")],
    3: [("6", "脚注22从正文切出")],
    4: [("0", "边缘文字应为 text 而非 equation"), ("5", "底部脚注23应为 page_footnote")],
    8: [("0", "顶部边缘文字与左栏正文分开"), ("1-11", "两栏脚注25-33独立识别并分项")],
    9: [("1-10", "两栏续脚注及34-36按区域与编号分项，不混入正文")],
    10: [("7,8", "右栏脚注84、85识别为两个 page_footnote")],
    11: [("3", "图注、正文与脚注86分开"), ("6", "脚注87、88分项")],
    12: [
        ("3,6", "合并完整 Figure5.1 图注"),
        ("4,8", "合并完整 Figure5.2 图注"),
        ("9", "正文与脚注41分开"),
        ("5", "两张有独立图注的图片分别保留"),
    ],
    13: [("9,10", "正文独立，脚注24与25各保留完整条目")],
    14: [("7,10,11,12", "脚注49-52分别识别为 page_footnote")],
    15: [("4", "正文与脚注72分开")],
    16: [("1", "目录 Introduction、Part I-IV 等条目保留原页粗体")],
    28: [("11,12", "公式(2)整体为 equation"), ("13,14", "公式(3)整体为 equation")],
    29: [("10,11,12", "块11正文部分接回10，公式(5)与编号12独立聚合")],
    30: [("9,10", "完整公式(10)"), ("14,15", "完整公式(12)"), ("12", "Definition4 与独立公式(11)分开")],
    31: [("1-6", "完整聚合公式(13)，保留分式与编号")],
    32: [("5", "Lagrange equation 应为 equation"), ("8", "底部编号1应为 page_footnote")],
    33: [("0", "Prologue 与罗马数字不应为 equation"), ("3-5", "聚合完整独立公式"), ("8,9", "脚注3、4分别识别")],
    34: [
        ("0", "边缘文字不应为 equation"),
        ("1", "正文与独立公式分开"),
        ("4,5", "合并完整公式"),
        ("7,9", "两处独立公式应为 equation"),
        ("12,13", "脚注5、6分别识别"),
    ],
    36: [("0", "章节标题应为 paragraph_title"), ("4", "Figure2.1 应为 image_caption")],
    37: [("0", "章节标题应为 paragraph_title"), ("6,7", "柱状图为 image，配套图注为 image_caption")],
    38: [
        ("61", "6.2章节标题应为 paragraph_title；原标块4映射至当前61"),
        ("63", "编号5应为 page_footnote；原标块7映射至当前63"),
    ],
}

NEW = {
    "7-": {
        1: [
            ("4", "按原页段界拆分正文，风险提示和投资建议各自独立"),
            ("12", "基础数据两列表为 table，不能为 equation"),
            ("15", "相关研究报告标题与报告条目分开"),
        ],
        2: [("1-12", "六幅图按编号1-6逐行阅读，图注与图体相邻")],
        3: [("1", "盈利预测调整说明标题、引导正文及四个编号段分别保留"), ("3", "纯栅格表格需OCR才可结构化", "能力范围外")],
        4: [("1,2", "两行章节标题合为 paragraph_title，首行不能为 equation")],
        5: [("2-15", "四个财务表分别识别，顶端表头年份归入所属表格")],
    },
    "2022.emnlp-main.614": {
        1: [("0", "题名与作者行分开，作者不能属于 doc_title")],
        2: [("6", "2 Related Work 与2.1标题分为两个 paragraph_title")],
        4: [("1", "3 Datasets 标题与正文分开")],
        5: [("6", "5 Methods 与5.1标题分别保留"), ("13,14", "expressed in Equation 1. 属于正文，不能进公式(1)")],
        6: [("10,11", "+ Query FT.接回上一段，Parameter Efficiency另起段"), ("16", "脚注7与8分别保留")],
        7: [
            ("0-4,12-15,17", "Table3 完整聚合，列名不应为header，分组行不应为脚注，不重叠拆表"),
            ("8", "6 Results、6.1标题与正文分开"),
        ],
        8: [("0-4,11,12", "Table4 列名与首个分组行归回完整表格"), ("18", "脚注10、11分项")],
        9: [("2", "续段与For the re-ranking models新段分开"), ("7", "8 Limitations 标题与正文分开")],
        14: [("0,5", "A Dataset Details、B 1st Stage Retrieval为独立章节标题"), ("9", "脚注13、14分项")],
        15: [
            ("0-19,21-29", "Table6 两个面板各自保持完整列名与分组，不拆为众多重叠表格"),
            ("10", "C Zero-Shot Baselines 为章节标题"),
            ("20,30,31", "Table6总图注与两面板说明关联该表"),
        ],
        16: [("0,4", "附录D、E为章节标题")],
        17: [
            ("0-4,11-14", "Table7完整聚合并纳入列名"),
            ("5-9,17,18", "Table8纳入列名及首分组行"),
            ("10,16", "附录F、G为章节标题"),
        ],
        18: [
            ("0", "附录H为章节标题"),
            ("1-14", "Table9完整聚合，分组标签与全部单元格归属唯一"),
            ("15", "Table9图注不能吞入下一章节I标题"),
            ("19", "Table10说明应为 table_caption"),
        ],
    },
}


def visible(value):
    """递归展开公开内容，只用于锚定已完成的视觉判定。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(visible(item) for item in value)
    return visible(value.get("content", "")) if isinstance(value, dict) else ""


def indices(label):
    """展开人工块号区间，保留遗漏区域的空编号。"""
    out = set()
    for part in label.split(",") if label != "-" else []:
        ends = list(map(int, part.split("-")))
        out.update(range(ends[0], ends[-1] + 1))
    return sorted(out)


def flatten(blocks):
    """保存所有父子块的真实编号、位置和完整可见文字。"""
    out = []
    for block in blocks:
        out.append({key: block.get(key) for key in ("index", "type", "bbox")} | {"text": visible(block.get("content", ""))})
        content = block.get("content")
        if isinstance(content, list):
            out.extend(flatten([item for item in content if isinstance(item, dict) and "bbox" in item]))
    return out


def issue(doc, page, position, label, expected, blocks, origin, status="确认问题", confidence="H"):
    """将人工判断与来源版本、区域和文字锚点绑定，不由输出生成期望。"""
    chosen = [block for block in blocks if block["index"] in indices(label)]
    return {
        "id": f"{doc}-p{page:03d}-{position:03d}",
        "page": page,
        "origin": origin,
        "status": status,
        "confidence": confidence,
        "repair_status": "pending" if status == "确认问题" else "excluded",
        "original_indices": indices(label),
        "current_indices": indices(label),
        "expected": expected,
        "anchors": [
            {"bbox": b["bbox"], "type": b["type"], "text_start": b["text"][:100], "text_end": b["text"][-100:]} for b in chosen
        ],
        "current_blocks": chosen,
    }


def decide_old(item):
    """执行用户明确的单页豁免及OCR边界，保留类型、成员和段界问题。"""
    expectation = item["expectation"]
    types = {b["type"] for b in item["current_blocks"]}
    marginal = re.search(r"header|footer|page_number|页眉|页脚|页码", expectation)
    local_error = re.search(r"正文段|脚注|page_footnote|equation|table|index|图框|正文text.*包含", expectation)
    if marginal and types <= {"text"} and not local_error:
        return "撤销", "单页边缘文字为text符合用户豁免"
    if item["confidence"] == "M":
        return "待人工裁决", "原初稿为建议或歧义，未升级为必修"
    return "确认问题", "原页与layout视觉证据明确"


def refine_cases(data):
    """拆分类型建议与真实边界缺陷，并补入原页直接观察的漏框区域。"""
    boundaries = {
        56: "六个圆点条目保持独立边界；原块1、2属于同一条目，原块3应拆成两项。",
        66: "从正文切出第一条 full-service restaurants，五项分别保持完整边界。",
        69: "从正文切出前两条圆点项目，第三项的两行续文归为同一条目。",
        104: "引导正文与第一条 acquisitions 项目分开，四项保持独立边界。",
        120: "表体从 Genes in DNA 表头开始；上方正文、圆点项目和引导句不得归入表格。",
        122: "操作步骤 3. Mix reagents 不能成为文档题名，普通文字类型可接受。",
        123: "引导正文与六个圆点条目分开，每项保留完整续行。",
        169: "材料清单和操作步骤的首项不能为章节标题，各项保持完整边界。",
        181: "右栏简介与第一项分开，合并在末块的两项拆开，共保留四项。",
    }
    missing_regions = {
        109: ([0.092, 0.124, 0.231, 0.14], "x=v·t (7)"),
        111: ([0.092, 0.699, 0.222, 0.721], "v=k/r (1)"),
        170: ([0.141, 0.607, 0.423, 0.623], "A4 = R × K × LS × Pc × Pt"),
        183: ([0.363, 0.441, 0.633, 0.93], "CustomerBERT; Recall@10; NDCG@10; 14.3%"),
    }
    for doc in data["documents"]:
        number = int(doc["name"][-3:]) if doc["name"].startswith("010300") else None
        extra = []
        for case in doc["cases"]:
            case["source_sha256"] = doc["sha256"]
            case["source_path"] = doc["path"]
            page = doc["pages"][case["page"] - 1]
            case["evidence"] = {
                "source": page["source_image"],
                "layout": page["layout_image"],
                "method": "原页与layout视觉对照；未读取GT",
            }
            if not case["anchors"] and number in missing_regions:
                bounds, text = missing_regions[number]
                case["anchors"] = [
                    {
                        "bbox": bounds,
                        "type": "visually_missing_region",
                        "text_start": text,
                        "text_end": text,
                        "text_source": "原页视觉转录，非解析输出",
                    }
                ]
                if number == 183:
                    case["anchors"].append(
                        {
                            "bbox": [0.687, 0.521, 0.927, 0.852],
                            "type": "visually_missing_region",
                            "text_start": "0.882; 0.735; 20%",
                            "text_end": "0.882; 0.735; 20%",
                            "text_source": "原页视觉转录",
                        }
                    )
            if case["status"] == "确认问题" and number in boundaries and re.search(r"list(?:_item)?", case["expected"]):
                suggestion = deepcopy(case)
                suggestion.update(
                    id=case["id"] + "-type",
                    status="待人工裁决",
                    repair_status="excluded",
                    expected="可选：清单采用 list 类型；独立 text 条目且边界准确可接受。",
                    reason="从复合记录中分离类型建议；不作为原生 Flash 必修项。",
                )
                extra.append(suggestion)
                original = case["expected"]
                case["expected"] = boundaries[number]
                case.setdefault("revision_log", []).append(
                    {"before": original, "after": case["expected"], "reason": suggestion["reason"]}
                )
        doc["cases"].extend(extra)


def render_gallery(output, data):
    """生成可编辑的逐页证据画廊，并允许导出用户修正后的JSON。"""
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    shell = """<!doctype html><meta charset="utf-8"><title>Flash合并复核</title>
<style>body{font:16px system-ui;margin:20px;background:#eef1f5}header{position:sticky;top:0;background:white;padding:12px;z-index:2}select,input,button{font:inherit;margin:4px}article{background:white;padding:12px;margin:16px 0}section{display:flex;gap:10px}figure{margin:0;flex:1;min-width:0}img{width:100%}textarea{width:99%;min-height:70px;font:15px system-ui}.issue{border-top:1px solid #ddd;padding:10px}small{color:#555}h2{font-size:18px}</style>
<header><b>Flash视觉合并复核</b> <span id="count"></span><input id="search" placeholder="样本号或问题关键词"><select id="filter"><option>全部</option><option>确认问题</option><option>待人工裁决</option><option>撤销</option><option>能力范围外</option></select><button id="export">导出修正JSON</button><div>单页边缘text可接受；全部来源为原页视觉判定，未使用benchmark GT。</div></header><details id="export-panel" hidden><summary>修正JSON（可复制保存）</summary><a id="download-json">下载修正JSON</a><button id="copy-json">复制完整JSON</button><span id="copy-result" role="status"></span><textarea id="export-json" aria-hidden="true" readonly></textarea></details><main id="pages"></main>
<script>const data=__DATA__;const pages=document.querySelector('#pages');
// 按文档和物理页展示证据；编辑值仅写入可下载副本。
function draw(){pages.replaceChildren();let count=0;const q=document.querySelector('#search').value.toLowerCase(),filter=document.querySelector('#filter').value;for(const doc of data.documents){for(const p of doc.pages){const cases=doc.cases.filter(x=>x.page===p.page);if(q&&!((doc.name+' '+cases.map(x=>x.expected).join(' ')).toLowerCase().includes(q)))continue;if(filter!=='全部'&&!cases.some(x=>x.status===filter))continue;count++;const a=document.createElement('article');const h=document.createElement('h2');h.textContent=doc.name+' · 物理页 '+p.page;a.append(h);const pair=document.createElement('section');for(const [label,path] of [['原页',p.source_image],['基线布局',p.layout_image],...(p.candidate_image?[['候选布局',p.candidate_image]]:[])]){const f=document.createElement('figure');const c=document.createElement('figcaption');c.textContent=label;const img=document.createElement('img');img.loading='lazy';img.src=path;f.append(c,img);pair.append(f)}a.append(pair);for(const x of cases){const d=document.createElement('div');d.className='issue';const s=document.createElement('select');for(const st of ['确认问题','待人工裁决','撤销','能力范围外']){const op=new Option(st,st);op.selected=x.status===st;s.add(op)}s.onchange=()=>x.status=s.value;const t=document.createElement('textarea');t.value=x.expected;t.oninput=()=>x.expected=t.value;const b=document.createElement('small');b.textContent=x.id+' · '+x.origin+' · 原块 '+x.original_indices.join(',')+' · '+(x.reason||'')+' · 当前块 '+x.current_indices.join(',')+' · 修复 '+x.repair_status;const evidence=document.createElement('details');const summary=document.createElement('summary');summary.textContent='区域、文字锚点与裁决依据';const pre=document.createElement('pre');pre.style.whiteSpace='pre-wrap';pre.textContent=JSON.stringify({source_sha256:doc.sha256,anchors:x.anchors,current_blocks:x.current_blocks,repair_verification:x.repair_verification},null,2);evidence.append(summary,pre);const reason=document.createElement('textarea');reason.placeholder='人工修正理由';reason.value=x.user_review||'';reason.oninput=()=>x.user_review=reason.value;d.append(s,b,t,reason,evidence);a.append(d)}pages.append(a)}}document.querySelector('#count').textContent=count+' / '+data.page_count+' 页'}
// 将完整修正副本复制到剪贴板，并显示成功或手动复制提示。
document.querySelector('#copy-json').onclick=async()=>{const result=document.querySelector('#copy-result');try{await navigator.clipboard.writeText(document.querySelector('#export-json').value);result.textContent='已复制完整JSON'}catch{document.querySelector('#export-json').select();result.textContent='请复制已选中的JSON'}};
document.querySelector('#search').oninput=draw;document.querySelector('#filter').onchange=draw;document.querySelector('#export').onclick=()=>{const content=JSON.stringify(data,null,2);const panel=document.querySelector('#export-panel'),a=document.querySelector('#download-json');if(a.getAttribute('href'))URL.revokeObjectURL(a.href);a.href=URL.createObjectURL(new Blob([content],{type:'application/json'}));a.download='annotations.corrected.json';document.querySelector('#export-json').value=content;panel.hidden=false;panel.open=true;a.click()};draw();</script>"""
    (output / "index.html").write_text(shell.replace("__DATA__", payload), encoding="utf-8")


def merge(args):
    """复制已有视觉证据并合并明确白名单，不扫描基准目录或读取GT。"""
    old = json.loads((args.initial / "annotations.initial.json").read_text())
    old_run = json.loads((args.initial / "run.json").read_text())
    manual_run = json.loads((args.diagnostic / "run.json").read_text())
    old_by_id = {d["id"]: d for d in old_run["documents"]}
    manual_by_id = {Path(d.get("source", d.get("source_pdf", ""))).stem: d for d in manual_run["documents"]}
    data = {
        "schema_version": 1,
        "gt_used": False,
        "single_page_marginal_text_accepted": True,
        "documents": [],
        "baseline_runs": [old_run, manual_run],
    }
    jobs = [(f"010300{n:08d}", args.diagnostic, MANUAL[n]) for n in MANUAL]
    jobs += [(s["id"], args.initial / "documents", s) for s in old["samples"]]
    jobs += [(name, args.diagnostic, notes) for name, notes in NEW.items()]
    for name, root, notes in jobs:
        source_folder = root / name
        target = args.output / "baseline" / name
        target.mkdir(parents=True, exist_ok=True)
        for path in source_folder.iterdir():
            if path.is_file() and (path.suffix in {".json", ".pdf", ".png"}):
                shutil.copy2(path, target / path.name)
        middle = json.loads((target / "middle.json").read_text())
        record = old_by_id.get(name) or manual_by_id.get(name)
        if record is None:
            record = next(d for d in manual_run["documents"] if d.get("id", d.get("name")) == name)
        source = record.get("source", record.get("source_pdf", record.get("path")))
        sha = hashlib.sha256(Path(source).read_bytes()).hexdigest()
        doc = {"name": name, "path": source, "sha256": sha, "page_count": len(middle["pages"]), "cases": [], "pages": []}
        for pi, page in enumerate(middle["pages"], 1):
            blocks = flatten(page["blocks"])
            if name in old_by_id:
                for i, item in enumerate(notes["issues"], 1):
                    status, reason = decide_old(item)
                    if name.endswith("00141") and item["confidence"] == "H":
                        status, reason = "能力范围外", "主体无原生字符，文字结构化需要OCR；图形保留另审"
                    if name.endswith("00184") and item["indices_label"] == "13-16":
                        status, reason = "能力范围外", "底部注释为栅格图片"
                    case = issue(
                        name,
                        pi,
                        i,
                        item["indices_label"],
                        item["expectation"],
                        blocks,
                        "AI初稿复核",
                        status,
                        item["confidence"],
                    )
                    case["reason"] = reason
                    doc["cases"].append(case)
            else:
                rows = notes.get(pi, []) if isinstance(notes, dict) else notes
                for i, row in enumerate(rows, 1):
                    case = issue(
                        name,
                        pi,
                        i,
                        row[0],
                        row[1],
                        blocks,
                        "新增PDF视觉复核" if name in NEW else "人工标注视觉复核",
                        row[2] if len(row) > 2 else "确认问题",
                    )
                    if name.endswith("00033"):
                        case["reason"] = "人工记录第二条32按原页及块3-5纠正为33"
                    doc["cases"].append(case)
            images = {}
            for label in ("source", "layout"):
                matches = sorted(target.glob(f"{label}*.png"))
                image_path = matches[pi - 1]
                images[f"{label}_image"] = str(image_path.relative_to(args.output))
            doc["pages"].append({"page": pi, "visual_review_completed": True, **images})
        data["documents"].append(doc)
    data["document_count"] = len(data["documents"])
    data["page_count"] = sum(d["page_count"] for d in data["documents"])
    assert (data["document_count"], data["page_count"]) == (187, 208)
    refine_cases(data)
    originals = args.output / "original-records"
    originals.mkdir(exist_ok=True)
    for path in (
        args.initial / "annotations.initial.json",
        args.initial / "visual_notes.tsv",
        args.initial.parent / "mark.txt",
    ):
        if path.is_file():
            shutil.copy2(path, originals / path.name)
    data["summary"] = dict(Counter(c["status"] for d in data["documents"] for c in d["cases"]))
    (args.output / "annotations.reviewed.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    lines = ["Flash合并视觉复核；187份PDF/208页；未使用benchmark GT。", "单页边缘text可接受；确认问题尚需修复与验收。", ""]
    for doc in data["documents"]:
        lines.append(doc["name"])
        lines.extend(
            f"  [{c['status']}] 页{c['page']} 块{','.join(map(str, c['current_indices']))}: {c['expected']}"
            for c in doc["cases"]
        )
        lines.append("")
    (args.output / "mark.reviewed.txt").write_text("\n".join(lines))
    render_gallery(args.output, data)
    print(json.dumps({k: data[k] for k in ("document_count", "page_count", "summary")}, ensure_ascii=False))


def main():
    """通过显式输入目录运行合并，所有读取都指向本轮已知产物。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initial", type=Path, required=True)
    parser.add_argument("--diagnostic", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    merge(args)


if __name__ == "__main__":
    main()
