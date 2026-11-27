"""用可移植源图真值检查 Flash 的真实图像资产及图外可见正文。"""

from __future__ import annotations

import argparse
from collections import defaultdict
import gzip
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import unquote

from bs4 import BeautifulSoup
import numpy as np
from PIL import Image

if __package__:
    from .score_flash_layout_gt import flat_blocks, geom, norm, pair_regions
else:
    from score_flash_layout_gt import flat_blocks, geom, norm, pair_regions

GT = Path(__file__).resolve().parents[1] / "tests/fixtures/flash_layout_gt_r15_graphic_holdout_v1.json.gz"
PAGE_BLOCKS = ".docvortex-block[data-page-idx][data-block-index]"
PROSE_EXCLUDE = {"image", "chart", "table", "equation", "interline_equation", "code"}
HEADING = {"title", "paragraph_title"}
ROUTES = ("native", "default")


def load(path):
    """读取纯 JSON 或确定性 gzip 夹具。"""
    raw = path.read_bytes()
    return json.loads(gzip.decompress(raw) if path.suffix == ".gz" else raw)


def fraction(box, outer):
    """计算源区域被候选框盖住的比例。"""
    width = max(0.0, min(box[2], outer[2]) - max(box[0], outer[0]))
    height = max(0.0, min(box[3], outer[3]) - max(box[1], outer[1]))
    area = (box[2] - box[0]) * (box[3] - box[1])
    return width * height / area if area > 0 else 0.0


def normalized_box(box):
    """将便于目视标注的千分数坐标换成解析器使用的页面比例。"""
    return [v / 1000 for v in box]


def main_dom(html):
    """提取实际可见的页块，并排除可折叠内容、替代文字和图注。"""
    soup = BeautifulSoup(html, "html.parser")
    removable = 'details:not([open]), [hidden], [aria-hidden="true"], script, style, template, noscript'
    for node in soup.select(removable):
        if node.parent is not None:
            node.decompose()
    for node in soup.select("[style]"):
        style = node.get("style", "")
        if node.parent is not None and re.search(r"(?:^|;)\s*(display\s*:\s*none|visibility\s*:\s*hidden)", style, re.I):
            node.decompose()
    blocks = defaultdict(dict)
    for node in soup.select(PAGE_BLOCKS):
        page = int(node["data-page-idx"]) + 1
        index = int(node["data-block-index"])
        images = [unquote(img["src"]).removeprefix("./") for img in node.select("img[src]")]
        typ = node["data-block-type"]
        if typ in PROSE_EXCLUDE:
            blocks[page][index] = {"type": typ, "text": "", "images": images}
            continue
        for excluded in node.select("figure, figcaption, img, [data-block-type$='_caption'], [data-block-type$='_footnote']"):
            if excluded.parent is not None:
                excluded.decompose()
        blocks[page][index] = {"type": typ, "text": norm(node.get_text(" ", strip=True)), "images": images}
    return blocks


def markdown_representation(markdown):
    """取得 Markdown 正文和真实图片引用，移除 HTML 图体或隐藏说明。"""
    image_pattern = r"!\[[^\]]*\]\(\s*<?([^\s)>]+)>?[^)]*\)"
    image_paths = [unquote(raw).removeprefix("./") for raw in re.findall(image_pattern, markdown)]
    image_paths.extend(
        unquote(raw).removeprefix("./") for raw in re.findall(r'<img\b[^>]*\bsrc=["\']([^"\']+)', markdown, flags=re.I)
    )
    body = re.sub(image_pattern, "", markdown)
    if "<" in body:
        body = re.sub(
            r"<(?:figure|figcaption|details)(?:\s[^>]*)?>.*?</(?:figure|figcaption|details)>", "", body, flags=re.I | re.S
        )
        body = BeautifulSoup(body, "html.parser").get_text(" ")
    body = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", body)
    return norm(body), image_paths


def visible_figures(page, dom, folder, md_paths):
    """逐实例核对公开图体、HTML 与 Markdown 的真实可解引用资产。"""
    rows = []
    for block in page["blocks"]:
        if block["type"] not in {"image", "chart"}:
            continue
        for body in block["content"]:
            if not body["type"].endswith("_body") or not body.get("image_path"):
                continue
            name = body["image_path"]
            path = (folder / name).resolve()
            if not path.is_relative_to(folder.resolve()):
                raise ValueError(f"无效资产路径 {name}")
            visible = dom.get(block["index"], {}).get("images", [])
            linked = path.is_file() and name in visible and name in md_paths
            row = dict(
                index=block["index"],
                path=name,
                bbox=body["bbox"],
                image=None,
                html=name in visible,
                markdown=name in md_paths,
                linked=linked,
            )
            if linked:
                try:
                    with Image.open(path) as image:
                        converted = image.convert("RGBA")
                        background = Image.new("RGBA", converted.size, "white")
                        background.alpha_composite(converted)
                        row["image"] = background.convert("RGB")
                    row["sha256"] = sha256(path.read_bytes()).hexdigest()
                except (OSError, ValueError) as error:
                    row.update(linked=False, asset_error=str(error))
            rows.append(row)
    return rows


def probe(point, crop):
    """检查落在已链接裁片内的源窗口是否仍有指定颜色或明暗墨迹。"""
    x, y = (v / 1000 for v in point["center"])
    radius = point["radius"] / 1000
    x0, y0, x1, y1 = crop["bbox"]
    image = crop["image"]
    if image is None or x < x0 or x > x1 or y < y0 or y > y1 or x1 <= x0 or y1 <= y0:
        return 0.0
    bounds = (
        max(0, round(image.width * (x - radius - x0) / (x1 - x0))),
        max(0, round(image.height * (y - radius - y0) / (y1 - y0))),
        min(image.width, round(image.width * (x + radius - x0) / (x1 - x0))),
        min(image.height, round(image.height * (y + radius - y0) / (y1 - y0))),
    )
    if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
        return 0.0
    colors = np.asarray(image.crop(bounds), dtype=np.int16)
    red, green, blue = colors.transpose(2, 0, 1)
    mode = point["mode"]
    if mode == "red":
        mask = (red > 95) & (red > green + 45) & (red > blue + 35)
    elif mode == "green":
        mask = (green > 75) & (green > red + 22) & (green > blue + 20)
    elif mode == "blue":
        mask = (blue > 100) & (blue > red + 25) & (blue > green + 12)
    elif mode == "color":
        mask = colors.max(2) - colors.min(2) > 30
    elif mode == "ink":
        mask = colors.min(2) < 160
    elif mode == "light_ink":
        mask = colors.min(2) > 160
    else:
        raise ValueError(f"未知墨迹模式 {mode}")
    return round(float(mask.mean()), 5)


def figure_score(ref, crops):
    """按图体相交和墨迹评估，同页独立图例可随图或单独出现。"""
    frame = normalized_box(ref["box"])
    details = []
    for crop in crops:
        overlap = geom(frame, crop["bbox"])
        covered = fraction(frame, crop["bbox"])
        if max(overlap, covered) < 0.12:
            continue
        witnesses = []
        for point in ref["points"]:
            actual = probe(point, crop)
            witnesses.append(
                dict(
                    note=point["note"],
                    actual=actual,
                    reference=point["reference_fraction"],
                    ok=actual >= max(0.009, 0.30 * point["reference_fraction"]),
                )
            )
        geometry = covered >= 0.70 if ref["role"] == "associated_legend" else overlap >= 0.50
        details.append(
            dict(
                path=crop["path"],
                index=crop["index"],
                sha256=crop.get("sha256"),
                iou=round(overlap, 6),
                coverage=round(covered, 6),
                geometry=geometry,
                linked=crop["linked"],
                witnesses=witnesses,
                pass_figure=crop["linked"] and geometry and all(w["ok"] for w in witnesses),
            )
        )
    chosen = max(
        details,
        key=lambda x: (x["pass_figure"], x["linked"], x["geometry"], sum(p["ok"] for p in x["witnesses"]), x["iou"]),
        default=None,
    )
    return dict(
        id=ref["id"],
        role=ref["role"],
        repeated=ref["repeated"],
        match=chosen,
        passed=bool(chosen and chosen["pass_figure"]),
        best_iou=max((x["iou"] for x in details if x["linked"]), default=0.0),
        nearby=details,
    )


def text_score(anchor, page_dom, md, page):
    """检查同一物理页上的可见外部正文、MiddleJson 几何及 Markdown。"""
    phrase = norm(anchor["text"])
    permitted = HEADING if anchor["role"] == "heading" else None
    blocks = [b for b in page_dom.values() if permitted is None or b["type"] in permitted]
    current = "".join(b["text"] for b in blocks)
    count = current.count(phrase)
    visible_count = "".join(b["text"] for b in page_dom.values()).count(phrase)
    bbox = normalized_box(anchor["box"]) if anchor["box"] else None
    middle = [
        b
        for b in flat_blocks(page)
        if b["text"]
        and b["type"] not in PROSE_EXCLUDE
        and not b["type"].endswith(("_caption", "_footnote"))
        and (permitted is None or b["type"] in permitted)
    ]
    local = [b for b in middle if bbox is None or max(fraction(bbox, b["bbox"]), fraction(b["bbox"], bbox)) >= 0.25]
    found_at_source = phrase in "".join(b["text"] for b in local)
    md_count = md.count(phrase)
    return dict(
        text=anchor["text"],
        role=anchor["role"],
        html_visible_count=visible_count,
        html_expected_role_count=count,
        md_body_count=md_count,
        middle_source_position=found_at_source,
        passed=count == 1 and md_count >= 1 and found_at_source,
    )


def score_document(document, folder, md_path, html_path):
    """逐页核对公开精标；失败仍留在完整分母里。"""
    status = load(folder / "status.json")
    if status["sha256"] != document["sha256"] or status["physical_pages"] != document["pages"]:
        raise ValueError(f"原件身份不符 {folder}")
    if status["status"] == "complete":
        middle = load(folder / "middle.json.gz")["pages"]
        all_dom = main_dom(html_path.read_text())
        md, md_paths = markdown_representation(md_path.read_text())
    elif status["status"] == "failed":
        middle = None
        all_dom = {}
        md, md_paths = "", []
    else:
        raise ValueError(f"回放未完成 {folder}")
    rows = {}
    for number, sample in document["annotated"].items():
        physical = int(number)
        page = middle[physical - 1] if middle else {"blocks": [], "page_idx": physical - 1}
        assert page["page_idx"] + 1 == physical
        dom = all_dom.get(physical, {})
        images = visible_figures(page, dom, folder, md_paths)
        negatives = []
        for zone in sample["nofig"]:
            bbox = normalized_box(zone)
            encroachment = [
                dict(path=i["path"], region_covered=round(fraction(bbox, i["bbox"]), 6))
                for i in images
                if i["linked"] and fraction(bbox, i["bbox"]) >= 0.12
            ]
            negatives.append(dict(box=zone, crops=encroachment, passed=bool(middle and not encroachment)))
        overlays = []
        for query in sample["forbidden"]:
            token = norm(query)
            found = [b["text"] for b in dom.values() if token == b["text"] or len(token) > 6 and token in b["text"]]
            overlays.append(dict(query=query, visible_instances=len(found), passed=bool(middle and not found)))
        primary = [{"id": f["id"], "bbox": normalized_box(f["box"])} for f in sample["figures"] if f["role"] == "figure"]
        _, placement_iou = pair_regions(primary, [i for i in images if i["linked"]])
        figures = [figure_score(ref, images) for ref in sample["figures"]]
        for f in figures:
            f["geometric_match_iou"] = round(placement_iou.get(f["id"], 0.0), 6)
        rows[number] = dict(
            parse=status["status"],
            figures=figures,
            anchor=[text_score(ref, dom, md, page) for ref in sample["keep"]],
            false_figures=negatives,
            overlay=overlays,
            crops=[{k: v for k, v in i.items() if k != "image"} for i in images],
        )
    return rows


def changed_outputs(old, new):
    """逐物理页比较内容字节，并核验导出的资产文件字节。"""
    states = [load(p / "status.json") for p in (old, new)]
    if any(s["status"] != "complete" for s in states):
        endings = [s.get("error", "").splitlines()[-1:] for s in states]
        return dict(succeeded=[s["status"] for s in states], error_tails=endings, same_failure=endings[0] == endings[1])
    one, two = (load(p / "digests.json") for p in (old, new))
    stats = {}
    for kind in ("model_pages", "middle_pages"):
        assert len(one[kind]) == len(two[kind]) == states[0]["physical_pages"]
        stats[kind] = [j for j, (a, b) in enumerate(zip(one[kind], two[kind], strict=True), 1) if a != b]
    for part in ("document.html", "document.md", "diagnostics.json"):
        stats[part] = (old / part).read_bytes() == (new / part).read_bytes()
    for part in ("model-pages.jsonl.gz", "middle.json.gz"):
        stats[part.removesuffix(".gz") + "_bytes_equal"] = gzip.decompress((old / part).read_bytes()) == gzip.decompress(
            (new / part).read_bytes()
        )
    before_names = set(one["assets"])
    after_names = set(two["assets"])
    same_names = before_names & after_names
    checked = set()
    for path in (old, new):
        for file in (path / "images").rglob("*"):
            if file.is_file():
                checked.add(file.relative_to(path).as_posix())
    all_assets = before_names | after_names | checked
    mismatch = []
    for name in sorted(all_assets):
        a, b = old / name, new / name
        if a.is_file() and b.is_file() and a.read_bytes() == b.read_bytes():
            continue
        mismatch.append(name)
    stats.update(
        succeeded=["complete", "complete"],
        assets_compared=len(same_names) if before_names == after_names else len(all_assets),
        changed_or_missing_asset_files=mismatch,
    )
    return stats


def summary(rows):
    """分别报告图体、图例、重复实例、正文和保护区的分子分母。"""
    records = [p for source in rows.values() for p in source.values()]
    figures = [f for p in records for f in p["figures"]]
    repeated = defaultdict(list)
    for item in figures:
        if item["repeated"]:
            repeated[item["repeated"]].append(item)
    return {
        "pages": len(records),
        "parsed_pages": sum(p["parse"] == "complete" for p in records),
        "primary_regions": [
            sum(f["passed"] for f in figures if f["role"] == "figure"),
            sum(f["role"] == "figure" for f in figures),
        ],
        "associated_legends": [
            sum(f["passed"] for f in figures if f["role"] == "associated_legend"),
            sum(f["role"] == "associated_legend" for f in figures),
        ],
        "geometry_primary_at_0.5": [
            sum(f["geometric_match_iou"] >= 0.5 for f in figures if f["role"] == "figure"),
            sum(f["role"] == "figure" for f in figures),
        ],
        "independent_text": [sum(a["passed"] for p in records for a in p["anchor"]), sum(len(p["anchor"]) for p in records)],
        "external_html_text": [
            sum(a["html_visible_count"] == 1 for p in records for a in p["anchor"]),
            sum(len(p["anchor"]) for p in records),
        ],
        "negative_prose_regions": [
            sum(a["passed"] for p in records for a in p["false_figures"]),
            sum(len(p["false_figures"]) for p in records),
        ],
        "watermark_visible_text_controls": [
            sum(a["passed"] for p in records for a in p["overlay"]),
            sum(len(p["overlay"]) for p in records),
        ],
        "repeated_raster_complete_sets": [sum(all(i["passed"] for i in values) for values in repeated.values()), len(repeated)],
        "repeated_raster_placements": [
            sum(i["passed"] for group in repeated.values() for i in group),
            sum(len(group) for group in repeated.values()),
        ],
    }


def teacher_score(gold, teacher_path):
    """让 PP 或 VLM 对相同原页区域做边界比较，不将模型框当作公开图像。"""
    totals = {}
    for name, suffix in (("pp", "json.gz"), ("vlm", "json")):
        rows = {}
        for slug, source in gold["sources"].items():
            for physical, truth in source["annotated"].items():
                path = teacher_path / name / f"d-{source['sha256'][:16]}-p{physical}.{suffix}"
                if not path.is_file():
                    continue
                result = load(path)
                assert result["image_sha256"] == truth["source_image_sha256"], path
                boxes = [
                    b.get("bbox_normalized", b["bbox"])
                    for b in result["boxes"]
                    if b.get("label", b.get("type")) in {"image", "chart", "seal"}
                ]
                primary = [{"id": f["id"], "bbox": normalized_box(f["box"])} for f in truth["figures"] if f["role"] == "figure"]
                _, overlaps = pair_regions(primary, [{"bbox": box} for box in boxes])
                rows[f"{slug}/{physical}"] = {f["id"]: round(overlaps.get(f["id"], 0.0), 6) for f in primary}
        totals[name] = {
            "pages": len(rows),
            "rows": rows,
            "geometry_primary_at_0.5": [
                sum(v >= 0.5 for p in rows.values() for v in p.values()),
                sum(len(p) for p in rows.values()),
            ],
        }
    return totals


def resolve_documents(roots, route, sources):
    """用身份一致的分批回放补齐同一臂，避免重算缓慢的完整文档。"""
    runtimes = [load(root / "runtime.json") for root in roots]
    for runtime in runtimes[1:]:
        if runtime != runtimes[0]:
            raise ValueError(f"同一臂的运行身份不一致 {roots}")
    folders = {}
    for slug, source in sources.items():
        for root in roots:
            folder = root / route / "documents" / source["sha256"]
            status = folder / "status.json"
            if status.is_file() and load(status)["status"] in {"complete", "failed"}:
                folders[slug] = folder
                break
        else:
            raise FileNotFoundError(f"缺少完整回放 {route}/{slug}")
    return folders


def main():
    """保存两臂两路报告，既有跨全篇字节比较也有源图级计分。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gt", type=Path, default=GT)
    parser.add_argument("--arm", action="append", required=True, metavar="NAME=DIR")
    parser.add_argument("--teacher-dir", type=Path)
    parser.add_argument("--only", action="append", help="分批复核时仅计已指定原件")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    gold = load(args.gt)
    assert gold["schema"] == "r15-graphic-holdout-v1"
    sources = {slug: source for slug, source in gold["sources"].items() if not args.only or slug in args.only}
    assert sources and (not args.only or len(sources) == len(set(args.only)))
    arms = defaultdict(list)
    for spec in args.arm:
        tag, path = spec.split("=", 1)
        arms[tag].append(Path(path).resolve())
    baseline = next(iter(arms))
    output = dict(gt_sha256=sha256(args.gt.read_bytes()).hexdigest(), sources=list(sources), routes={})
    for route in ROUTES:
        by_arm = {}
        diff = {}
        for arm, paths in arms.items():
            folders = resolve_documents(paths, route, sources)
            rows = {
                slug: score_document(sources[slug], folder, folder / "document.md", folder / "document.html")
                for slug, folder in folders.items()
            }
            by_arm[arm] = dict(
                summary=summary(rows), documents=rows, used_folders={slug: str(path) for slug, path in folders.items()}
            )
            if arm != baseline:
                old = by_arm[baseline]["used_folders"]
                diff[arm] = {slug: changed_outputs(Path(old[slug]), folders[slug]) for slug in sources}
        output["routes"][route] = dict(arms=by_arm, delta_from_first_arm=diff)
    if args.teacher_dir:
        output["independent_label_boundaries"] = teacher_score(gold, args.teacher_dir)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {r: {a: v["summary"] for a, v in d["arms"].items()} for r, d in output["routes"].items()},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
