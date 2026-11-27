"""给两组 Flash 公共 MiddleJson 计分，输出逐页、分来源及文档家族配对指标。

仅该离线工具的一对一匹配需要 SciPy，可在评测环境安装 scipy。
多个 --baseline-dir 和 --candidate-dir 可分别指向主语料与补充语料的重放目录。
用 --manifest 或 --source-root 提供原始 PDF，按 SHA256 验证后提取源文字锚点。
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import html
import json
from pathlib import Path
import re
import statistics
import unicodedata

from bs4 import BeautifulSoup
import numpy as np

if __package__:
    from .verify_flash_layout_gt import DEFAULT_GT, Gold, locate_sources, read_json
else:
    from verify_flash_layout_gt import DEFAULT_GT, Gold, locate_sources, read_json


WEIGHTS = {"content": 0.30, "ownership": 0.30, "order": 0.20, "structure_geometry": 0.20}
GRAPHIC_TYPES = {"image", "chart", "table", "code"}
ALIAS = {"chart": "image", "chart_caption": "image_caption", "chart_footnote": "image_footnote", "ref_text": "text"}


def norm(text):
    """把源锚点和可公开提取的文本转成一致的字母数字序列。"""
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def inner_text(value):
    """递归读取公开内容，仅从文本和表格 HTML 提取可见字，不把路径和诊断当作内容。"""
    if isinstance(value, list):
        return " ".join(inner_text(v) for v in value)
    if isinstance(value, dict):
        return inner_text(value.get("content"))
    if not isinstance(value, str):
        return ""
    if re.search(r"<(?:table|tr|td|th|tbody)\b", value, re.I):
        return html.unescape(re.sub(r"<[^>]*>", " ", value))
    return value


def flat_blocks(page):
    """展开列表条目与图表注释，视觉父块和 body 使用同一几何身份。"""
    result = []

    def visit(block, parent=None):
        """保留公开阅读顺序与父容器，避免把子块复制为第二个检测。"""
        typ = block["type"]
        body = block.get("content")
        if typ == "list" and isinstance(body, list):
            children = [child for child in body if isinstance(child, dict) and child.get("bbox")]
            if children:
                for child in children:
                    visit(child, typ)
                return
        if block.get("bbox") is None:
            return
        primary = body
        if typ in GRAPHIC_TYPES:
            bodies = [x for x in (body or []) if isinstance(x, dict) and x.get("type", "").endswith("_body")]
            primary = bodies[0].get("content") if bodies else body
        result.append(
            {
                "type": ALIAS.get(typ, typ),
                "bbox": block["bbox"],
                "index": block.get("index"),
                "text": norm(inner_text(primary)),
                "parent": parent,
                "html": primary if typ == "table" and isinstance(primary, str) else "",
            }
        )
        if typ in GRAPHIC_TYPES and isinstance(body, list):
            for child in body:
                if isinstance(child, dict) and child.get("type", "").endswith(("_caption", "_footnote")):
                    visit(child, block.get("index"))

    for block in page.get("blocks", []):
        visit(block)
    return result


def geom(a, b):
    """计算两个归一化轴对齐框的交并比。"""
    x = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    y = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area = x * y
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - area
    return area / union if union > 0 else 0.0


def page_glyphs(path, physical):
    """从原件抽出可见字的绘制位置，坐标用所选原页的实际可见宽高归一化。"""
    from docvortex.document.pdf import PDFDocument

    with PDFDocument(str(path)) as doc:
        page = doc[physical - 1]
        w, h = page.size
        source = page.get_chars_with_geometry().chars
    chars = []
    for c in source:
        text = norm(c.get("char", ""))
        if not text or c.get("is_generated") or c.get("text_render_mode") in (3, 7):
            continue
        box = c.get("tight_bbox") or c.get("bbox")
        if box is None:
            continue
        x0, y0, x1, y1 = map(float, box)
        if not (x1 > x0 and y1 > y0):
            continue
        chars.append((text, (x0 + x1) / (2 * w), (y0 + y1) / (2 * h), max((y1 - y0) / h, 0.001), (x1 - x0) / w))
    return chars


def source_table_anchors(regions, chars):
    """每个适用 GT 区域取最多三个独立文本串，屏蔽重复字串、跨行、失配及非文本区域。"""
    stream = "".join(c[0] for c in chars)
    result = {}
    for r in regions:
        x0, y0, x1, y1 = r["bbox"]
        runs = []
        current = ""
        prev = None
        for text, x, y, h, w in chars:
            if not x0 <= x <= x1 or not y0 <= y <= y1:
                if current:
                    runs.append(current)
                current = ""
                prev = None
                continue
            if prev is not None:
                px, py, ph, pw = prev
                if abs(y - py) > max(h, ph) * 0.55 or x < px - 0.004 or x - px > max(0.025, 3 * max(pw, w)):
                    runs.append(current)
                    current = ""
            current += text
            prev = x, y, h, w
        if current:
            runs.append(current)
        windows = []
        for line in runs:
            if len(line) < 4:
                continue
            size = min(7, len(line))
            for left in sorted({0, (len(line) - size) // 2, len(line) - size}):
                word = line[left : left + size]
                if stream.count(word) == 1:
                    windows.append(word)
        if windows:
            loc = sorted({0, (len(windows) - 1) // 2, len(windows) - 1})
            result[r["id"]] = [windows[i] for i in loc]
    counts = Counter(w for words in result.values() for w in set(words))
    return {k: list(dict.fromkeys(w for w in words if counts[w] == 1)) for k, words in result.items()}


def pair_regions(regions, pred):
    """以几何为依据做一对一最大匹配，最低匹配 IoU 与类别判断独立。"""
    if not regions or not pred:
        return {}, {}
    try:
        from scipy.optimize import linear_sum_assignment
    except ImportError as error:
        raise RuntimeError("运行一对一布局匹配需安装 scipy") from error
    ious = np.array([[geom(g["bbox"], p["bbox"]) for p in pred] for g in regions])
    utility = np.where(ious >= 0.20, ious, 0.0)
    rr, cc = linear_sum_assignment(-utility)
    pairs = {regions[i]["id"]: int(j) for i, j in zip(rr, cc) if utility[i, j] > 0}
    overlaps = {regions[i]["id"]: float(ious[i, j]) for i, j in zip(rr, cc) if utility[i, j] > 0}
    return pairs, overlaps


def match_scored_regions(regions, pred):
    """先匹配精确框，再用粗定位框寻找剩余预测以评价细类、文字、成员或顺序。"""
    exact = [region for region in regions if "bbox" in region["scored_fields"]]
    coarse = [region for region in regions if "bbox" not in region["scored_fields"]]
    pairs, overlaps = pair_regions(exact, pred)
    if coarse:
        used = set(pairs.values())
        remaining = [index for index in range(len(pred)) if index not in used]
        additional, locations = pair_regions(coarse, [pred[index] for index in remaining])
        pairs.update({key: remaining[index] for key, index in additional.items()})
        overlaps.update(locations)
    return pairs, overlaps


def mean_or_none(rows):
    """仅在确实存在独立标签时计入平均值。"""
    return statistics.fmean(rows) if rows else None


def matches_table_dimensions(raw, shape):
    """用直接表行和跨列单元格核验行列数，破损的跨度记为该维度不通过。"""
    if "<tr" not in raw.lower():
        return False
    rows = BeautifulSoup(raw, "html.parser").find_all("tr")
    if len(rows) != shape["rows"]:
        return False
    columns = 0
    for row in rows:
        try:
            spans = [int(cell.get("colspan", "1")) for cell in row.find_all(["td", "th"], recursive=False)]
        except (TypeError, ValueError):
            return False
        if any(span <= 0 for span in spans):
            return False
        columns = max(columns, sum(spans))
    return columns == shape["columns"]


def score_page(gt, page, anchors):
    """分别检查公开文本、成员归属、已标顺序和可见结构，未标区域不作负项。"""
    pred = flat_blocks(page)
    regions = gt["regions"]
    pairs, ious = match_scored_regions(regions, pred)
    words = {word for g in regions for word in anchors.get(g["id"], [])}
    present = {word: [i for i, p in enumerate(pred) for unused in range(p["text"].count(word))] for word in words}
    content = [int(len(hits) == 1) for hits in present.values()]
    ownership = []
    for reg in regions:
        key = reg["id"]
        marked = set(anchors.get(key, []))
        if not marked:
            continue
        pi = pairs.get(key)
        if pi is None:
            ownership.append(0.0)
            continue
        hits = {w for w, indices in present.items() if pi in indices}
        ownership.append(2 * len(hits & marked) / (len(hits) + len(marked)))
    region_ids = {region["id"] for region in regions}
    relation_scores = []
    for rel in gt.get("relations", []):
        if rel["kind"] in {"caption_of", "footnote_of"}:
            for child, body in rel.get("pairs", []):
                if child not in region_ids or body not in region_ids:
                    continue
                a, b = pairs.get(child), pairs.get(body)
                relation_scores.append(
                    int(
                        a is not None
                        and b is not None
                        and pred[a]["parent"] is not None
                        and pred[a]["parent"] == pred[b]["index"]
                    )
                )
        if rel["kind"] == "outside_bodies":
            for child in rel.get("regions", []):
                for body in rel.get("bodies", []):
                    if child not in region_ids or body not in region_ids:
                        continue
                    a, b = pairs.get(child), pairs.get(body)
                    if a is None or b is None:
                        relation_scores.append(0)
                        continue
                    c = pred[a]["bbox"]
                    d = pred[b]["bbox"]
                    cx = (c[0] + c[2]) / 2
                    cy = (c[1] + c[3]) / 2
                    relation_scores.append(int(a != b and not (d[0] <= cx <= d[2] and d[1] <= cy <= d[3])))
    ownership.extend(relation_scores)
    decisions = []
    for rel in gt.get("relations", []):
        if rel["kind"] != "before":
            continue
        for left, right in rel.get("pairs", []):
            if left not in region_ids or right not in region_ids:
                continue
            a = pairs.get(left)
            b = pairs.get(right)
            ai = pred[a]["index"] if a is not None else None
            bi = pred[b]["index"] if b is not None else None
            decisions.append(int(ai is not None and bi is not None and ai < bi))
    types = []
    boxes = []
    tables = []
    for reg in regions:
        key = reg["id"]
        pi = pairs.get(key)
        box = ious.get(key, 0)
        typ = reg.get("type")
        labels = reg.get("scored_fields", ["bbox", "type"])
        if "bbox" in labels:
            boxes.append(box)
        if typ and "type" in labels:
            types.append(int(pi is not None and pred[pi]["type"] == ALIAS.get(typ, typ)))
        if typ == "table" and "bbox" in labels:
            tables.append(box if pi is not None and pred[pi]["type"] == "table" else 0.0)
    geometry = mean_or_none(boxes)
    type_score = mean_or_none(types)
    exhaustive = dict.fromkeys(ALIAS.get(label, label) for label in gt.get("exhaustive_types", []))
    inventories = []
    for typ in exhaustive:
        marked = [r for r in regions if "type" in r["scored_fields"] and ALIAS.get(r.get("type"), r.get("type")) == typ]
        proposed = sum(b["type"] == typ for b in pred)
        tp = sum(
            r["id"] in pairs
            and pred[pairs[r["id"]]]["type"] == typ
            and ("bbox" not in r["scored_fields"] or ious[r["id"]] >= 0.5)
            for r in marked
        )
        inventories.append(2 * tp / (len(marked) + proposed) if marked or proposed else 1.0)
    if gt.get("blank_verified"):
        # 真空白页的清点范围覆盖整页，任何幻觉块都应使该页库存不通过。
        inventories = [float(not page.get("blocks"))]
    cell_shapes = []
    for r in regions:
        shape = r.get("table_dimensions")
        if not shape or "table_dimensions" not in r["scored_fields"]:
            continue
        pi = pairs.get(r["id"])
        raw = pred[pi]["html"] if pi is not None else ""
        cell_shapes.append(float(matches_table_dimensions(raw, shape)))
    quality = mean_or_none(
        [v for v in (geometry, type_score, mean_or_none(inventories), mean_or_none(cell_shapes)) if v is not None]
    )
    dims = {
        "content": mean_or_none(content),
        "ownership": mean_or_none(ownership),
        "order": mean_or_none(decisions),
        "structure_geometry": quality,
    }
    observed = sum(WEIGHTS[k] for k, v in dims.items() if v is not None)
    total = sum(WEIGHTS[k] * v for k, v in dims.items() if v is not None) / observed if observed else None
    return {
        "dimensions": dims,
        "score": total,
        "table_geometry": mean_or_none(tables),
        "table_count_gt": len(tables),
        "predicted": len(pred),
        "region_count": len(regions),
        "bbox_count": len(boxes),
        "matched": len(pairs),
        "anchor_count": len(words),
        "owners_count": len(ownership),
        "order_count": len(decisions),
        "type_count": len(types),
        "cell_shapes": cell_shapes,
        "ownership_relations_scored": len(relation_scores),
        "exhaustive_inventory": inventories,
        "full_page_extras_penalty_enabled": bool(exhaustive or gt.get("blank_verified")),
    }


def sample_family_deltas(rng, delta):
    """对同家族 A/D 差值配对重采样 2,500 次，保持分层混合的独立抽样。"""
    return delta[rng.integers(len(delta), size=(2500, len(delta)))].mean(axis=1)


def aggregate(records):
    """在各来源内先平均同一文档的页面，再给予每个家族等权。"""

    def family_scores(items, metric):
        """按单项可评家族生成配对分数，未标注的字段不计入。"""
        groups = defaultdict(lambda: defaultdict(list))
        for r in items:
            vals = [r[v].get(metric) if metric in r[v] else r[v]["dimensions"].get(metric) for v in ("A", "D")]
            if None not in vals:
                groups[r["family_id"]][r["document_id"]].append(vals)
        return {f: np.mean([np.mean(rows, axis=0) for rows in docs.values()], axis=0) for f, docs in groups.items()}

    result = {}
    for sp in ("all", "dev", "validation", "test", "validation+test"):
        subset = [r for r in records if sp == "all" or r["split"] in sp.split("+")]
        summary = {}
        rng = np.random.default_rng(20261120)
        for metric in ("score", *WEIGHTS, "table_geometry"):
            metric_out = {}
            for cohort in ("historical_badcase", "historical_control", "independent_regular", "historical_combined"):
                group = [
                    r
                    for r in subset
                    if r["cohort"] == cohort or (cohort == "historical_combined" and r["cohort"].startswith("historical"))
                ]
                vals = family_scores(group, metric)
                if not vals:
                    continue
                pairs = np.asarray(list(vals.values()))
                delta = pairs[:, 1] - pairs[:, 0]
                estimates = sample_family_deltas(rng, delta)
                metric_out[cohort] = {
                    "families": len(vals),
                    "A": round(pairs[:, 0].mean() * 100, 5),
                    "D": round(pairs[:, 1].mean() * 100, 5),
                    "delta": round(delta.mean() * 100, 5),
                    "ci95": np.round(np.percentile(estimates, [2.5, 97.5]) * 100, 5).tolist(),
                }
            if metric == "score":
                hi = family_scores([r for r in subset if r["cohort"].startswith("historical")], metric)
                no = family_scores([r for r in subset if r["cohort"] == "independent_regular"], metric)
                if hi and no:
                    mixed = {}
                    for w in (0.7, 0.5):
                        hv, nv = np.asarray(list(hi.values())), np.asarray(list(no.values()))
                        est = np.mean(hv, axis=0) * (1 - w) + np.mean(nv, axis=0) * w
                        dh = hv[:, 1] - hv[:, 0]
                        dn = nv[:, 1] - nv[:, 0]
                        bs = (1 - w) * sample_family_deltas(rng, dh) + w * sample_family_deltas(rng, dn)
                        mixed[str(w)] = {
                            "A": round(est[0] * 100, 5),
                            "D": round(est[1] * 100, 5),
                            "delta": round((est[1] - est[0]) * 100, 5),
                            "ci95": np.round(np.percentile(bs, [2.5, 97.5]) * 100, 5).tolist(),
                        }
                    metric_out["mixture_normal_share"] = mixed
            summary[metric] = metric_out
        result[sp] = summary
    return result


def region_anchors(regions, pdf_path, physical_page):
    """复用已冻结的可见字锚点，只为缺少锚点的表区从独立原页补充最多三个唯一串。"""
    anchors = {
        region["id"]: list(
            dict.fromkeys(norm(item["text"]) for item in region.get("content_anchors", []) if norm(item["text"]))
        )
        for region in regions
    }
    tables = [region for region in regions if region.get("type") == "table"]
    if any(not anchors[r["id"]] for r in tables):
        supplemental = source_table_anchors(tables, page_glyphs(pdf_path, physical_page))
        for key, words in supplemental.items():
            if not anchors[key]:
                anchors[key] = words
    counts = Counter(word for words in anchors.values() for word in words)
    return {key: [word for word in words if counts[word] == 1] for key, words in anchors.items()}


def read_replay(directories, gt_page):
    """按源 ID 定位唯一公开重放文档，校验物理页数和存在的运行身份。"""
    docid = gt_page["document_id"]
    matches = []
    for root in dict.fromkeys(directory.resolve() for directory in directories):
        folder = root / "documents" / docid
        matches.extend(path for path in (folder / "middle.json.gz", folder / "middle.json") if path.is_file())
    if len(matches) != 1:
        raise ValueError(f"{docid} 需要唯一的重放结果，实际匹配 {len(matches)} 个")
    middle = matches[0]
    status = middle.parent / "status.json"
    if status.is_file():
        state = json.loads(status.read_bytes())
        if state.get("status") != "complete" or state.get("sha256") != gt_page["pdf_sha256"]:
            raise ValueError(f"{status} 未完成或源指纹不符")
    pages = json.loads(read_json(middle))["pages"]
    if len(pages) != gt_page["page_count"]:
        raise ValueError(f"{middle} 的完整文档页数与 GT 不符")
    return pages


def compare(gold: Gold, paths: dict[str, Path], baseline_dirs: list[Path], candidate_dirs: list[Path]) -> dict:
    """按文档读取两个版本，对同一组可评标签配对计分，最后按文档家族聚合。"""
    groups = defaultdict(list)
    for page in gold.pages:
        groups[page.document_id].append(page.model_dump(mode="json"))
    records = []
    for group in groups.values():
        baseline = read_replay(baseline_dirs, group[0])
        candidate = read_replay(candidate_dirs, group[0])
        for gt in group:
            page_idx = gt["physical_page"] - 1
            anchors = region_anchors(gt["regions"], paths[gt["pdf_sha256"]], gt["physical_page"])
            current = score_page(gt, baseline[page_idx], anchors)
            changed = baseline[page_idx] != candidate[page_idx]
            proposed = score_page(gt, candidate[page_idx], anchors) if changed else current
            identity = {key: gt[key] for key in ("document_id", "family_id", "physical_page", "cohort", "split")}
            records.append({**identity, "A": current, "D": proposed, "middle_changed": changed})
    return {"gt_sha256": gold.annotation_sha256, "pages": records, "summary": aggregate(records)}


def main() -> None:
    """从校验过的便携 GT、原件与两组公共重放生成完整配对结果。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gt", type=Path, default=DEFAULT_GT, help="已冻结的布局 GT（JSON 或 JSON.GZ）")
    parser.add_argument("--manifest", type=Path, action="extend", nargs="+", default=[], help="含原件路径及 SHA256 的清单")
    parser.add_argument("--source-root", type=Path, action="extend", nargs="+", default=[], help="按源指纹检索的 PDF 目录")
    parser.add_argument(
        "--baseline-dir", type=Path, action="extend", nargs="+", required=True, help="包含 documents 的基线重放目录"
    )
    parser.add_argument(
        "--candidate-dir", type=Path, action="extend", nargs="+", required=True, help="包含 documents 的候选重放目录"
    )
    parser.add_argument("--output", type=Path, required=True, help="写入 JSON 评分结果")
    args = parser.parse_args()
    if not args.manifest and not args.source_root:
        parser.error("需要 --manifest 或 --source-root 定位经过 SHA256 核验的原件")
    gold = Gold.model_validate_json(read_json(args.gt))
    paths = locate_sources(gold.pages, args.manifest, args.source_root)
    output = args.output.resolve()
    if output in {args.gt.resolve(), *(path.resolve() for path in args.manifest), *paths.values()}:
        parser.error("输出路径与 GT、原件或输入清单重合")
    if any(output.is_relative_to((root / "documents").resolve()) for root in (*args.baseline_dir, *args.candidate_dir)):
        parser.error("评分输出不能覆盖输入重放文档")
    result = compare(gold, paths, args.baseline_dir, args.candidate_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"已评分 {len(result['pages'])} 页，结果保存在 {output}")


if __name__ == "__main__":
    main()
