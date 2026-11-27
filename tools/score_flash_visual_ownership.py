"""按可移植源图真值检查图形所有权、墨迹重复与可见图外文本。"""

from __future__ import annotations

import argparse
import base64
from collections import defaultdict
import gzip
from hashlib import sha256
import json
from pathlib import Path
import re

from bs4 import BeautifulSoup
import numpy as np
from PIL import Image

if __package__:
    from .score_flash_graphic_holdout import (
        PAGE_BLOCKS,
        ROUTES,
        changed_outputs,
        fraction,
        load,
        main_dom,
        markdown_representation,
        resolve_documents,
        visible_figures,
    )
    from .score_flash_layout_gt import flat_blocks, geom, norm, pair_regions
else:
    from score_flash_graphic_holdout import (
        PAGE_BLOCKS,
        ROUTES,
        changed_outputs,
        fraction,
        load,
        main_dom,
        markdown_representation,
        resolve_documents,
        visible_figures,
    )
    from score_flash_layout_gt import flat_blocks, geom, norm, pair_regions

TEXT_TYPES = {
    "heading": {"doc_title", "title", "paragraph_title"},
    "body": {
        "text",
        "list",
        "list_item",
        "ref_text",
        "paragraph_title",
        "doc_title",
        "title",
        "header",
        "footer",
        "aside_text",
    },
}
NON_VISIBLE = 'details:not([open]), [hidden], [aria-hidden="true"], script, style, template, noscript'


def envelope(parts, margin):
    """返回同一合法图组的边界并给裁片少量留白。"""
    return [
        max(0.0, min(part["box"][0] for part in parts) - margin),
        max(0.0, min(part["box"][1] for part in parts) - margin),
        min(1.0, max(part["box"][2] for part in parts) + margin),
        min(1.0, max(part["box"][3] for part in parts) + margin),
    ]


def witness_match(witness, crop, criteria):
    """在真实公开图片上定位同一物理窗口，核对源形状与颜色。"""
    image = crop["image"]
    box = witness["box"]
    x0, y0, x1, y1 = crop["bbox"]
    if image is None or x1 <= x0 or y1 <= y0 or fraction(box, crop["bbox"]) < criteria["witness_coverage"]:
        return {"score": 0.0, "passed": False}
    edge = witness["edge"]
    sx, sy = image.width / (x1 - x0), image.height / (y1 - y0)
    dx, dy = (box[2] - box[0]) * sx / edge, (box[3] - box[1]) * sy / edge
    offset_x, offset_y = (box[0] - x0) * sx, (box[1] - y0) * sy
    target = np.frombuffer(base64.b64decode(witness["rgb_b64"]), dtype=np.uint8).reshape(edge, edge, 3).astype(np.int16)
    mask = (
        np.unpackbits(np.frombuffer(base64.b64decode(witness["mask_b64"]), dtype=np.uint8))[: edge * edge]
        .reshape(edge, edge)
        .astype(bool)
    )
    assert int(mask.sum()) == witness["pixel_count"]
    tolerance = criteria["pixel_color_tolerance"]
    threshold = criteria[f"match_fraction_{witness['mode']}"]
    scores = []
    # 公开裁片框可能按页面千分位取整，允许同一原点周围的亚像素配准。
    jitter_x, jitter_y = min(1.25, max(0.5, dx / 2)), min(1.25, max(0.5, dy / 2))
    candidates = ((x * jitter_x, y * jitter_y) for x in (-1, 0, 1) for y in (-1, 0, 1))
    for shift_x, shift_y in candidates:
        left, top = offset_x + shift_x, offset_y + shift_y
        bounds = (max(0, left), max(0, top), min(image.width, left + dx * edge), min(image.height, top + dy * edge))
        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            continue
        found = image.resize((edge, edge), Image.Resampling.LANCZOS, box=bounds)
        distance = np.abs(np.asarray(found, dtype=np.int16) - target).max(2)
        scores.append(float((distance[mask] <= tolerance).mean()))
    score = max(scores, default=0.0)
    return {"score": round(score, 5), "passed": score >= threshold}


def part_result(part, scope, crops, criteria):
    """允许同图组共用裁片，同时查找第二份承载相同物理内容的裁片。"""
    matches = []
    for crop in crops:
        covered = fraction(part["box"], crop["bbox"])
        if covered < 0.10:
            continue
        probes = [{"name": witness["name"], **witness_match(witness, crop, criteria)} for witness in part["witnesses"]]
        in_scope = fraction(crop["bbox"], scope)
        admissible = (
            crop["linked"]
            and covered >= criteria["part_coverage"]
            and in_scope >= criteria["owner_area_within_scope"]
            and all(probe["passed"] for probe in probes)
        )
        matches.append(
            {
                "index": crop["index"],
                "path": crop["path"],
                "bbox": crop["bbox"],
                "linked": crop["linked"],
                "coverage": round(covered, 5),
                "iou": round(geom(part["box"], crop["bbox"]), 5),
                "inside_group_scope": round(in_scope, 5),
                "witnesses": probes,
                "admissible": admissible,
            }
        )
    owners = [m for m in matches if m["admissible"]]
    duplicate = []
    for point in part["witnesses"]:
        copies = [m for m in matches if any(w["name"] == point["name"] and w["passed"] for w in m["witnesses"])]
        if len(copies) > 1:
            duplicate.append(
                {"witness": point["name"], "owners": [m["index"] for m in copies], "paths": [m["path"] for m in copies]}
            )
    return {
        "id": part["id"],
        "role": part["role"],
        "owners": owners,
        "duplicate_witnesses": duplicate,
        "passed": bool(owners and not duplicate),
        "nearby": matches,
    }


def html_text(html):
    """读取真实公开 HTML 的可见块及独立图注文本。"""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select(NON_VISIBLE):
        if node.parent is not None:
            node.decompose()
    for node in soup.select("[style]"):
        style = node.get("style", "")
        if node.parent is not None and re.search(r"(?:^|;)\s*(display\s*:\s*none|visibility\s*:\s*hidden)", style, re.I):
            node.decompose()
    pages = defaultdict(list)
    for node in soup.select(PAGE_BLOCKS):
        page = int(node["data-page-idx"]) + 1
        index = int(node["data-block-index"])
        typ = node["data-block-type"]
        if typ in {"image", "chart", "table"}:
            for caption in node.select('figcaption, caption, .docvortex-caption[data-block-type$="_caption"]'):
                # 只记录本图表的一份可见题注；标准标签内的样式段落共享同一份文字。
                if caption.find_parent(["figcaption", "caption"]) or caption.find_parent(class_="docvortex-block") is not node:
                    continue
                pages[page].append((index, "caption", norm(caption.get_text(" ", strip=True))))
            if typ != "table":
                continue
        if node.find_parent(attrs={"data-page-idx": True, "data-block-index": True}) is not None:
            continue
        pages[page].append((index, typ, norm(node.get_text(" ", strip=True))))
    return pages


def public_text_blocks(page):
    """展开 MiddleJson 的列表成员，并保存外层 HTML 所用的公开索引。"""
    blocks = []
    for top in page["blocks"]:
        for item in flat_blocks({"blocks": [top]}):
            item["public_index"] = top.get("index")
            blocks.append(item)
    return blocks


def local_text_hits(ref, rows, visible, criteria):
    """用源文字几何判定所有者，区分同页不同物理位置的同词文本。"""
    query = norm(ref["text"])
    if ref["role"] == "caption":
        roles = {"title", "paragraph_title", "text", "caption"}
    else:
        roles = TEXT_TYPES.get(ref["role"], TEXT_TYPES["body"])
    eligible = [
        block
        for block in rows
        if block["text"]
        and max(fraction(ref["box"], block["bbox"]), fraction(block["bbox"], ref["box"])) >= criteria["text_position_overlap"]
    ]
    owners, fragments = [], []
    caption_types = {"caption"} if ref["role"] == "caption" else set()
    allowed = TEXT_TYPES["body"] | caption_types
    for block in eligible:
        typ = block["type"]
        if typ.endswith("_caption"):
            typ = "caption"
        if typ not in allowed:
            continue
        for index, public_type, value in visible:
            if index != block["public_index"] or public_type not in allowed:
                continue
            info = {
                "index": block["index"],
                "public_index": index,
                "type": typ,
                "expected_role": typ in roles and public_type in roles,
            }
            if query in block["text"] and query in value:
                owners.append(info | {"count": block["text"].count(query)})
                break
            # 被拆成多行的确切短标签仍可独立可读，向相邻块借词会被源框否决。
            if (
                len(block["text"]) >= 2
                and block["text"] in query
                and block["text"] in value
                and fraction(block["bbox"], ref["box"]) >= criteria["text_position_overlap"]
            ):
                fragments.append((block["text"], info))
                break
    if owners or len(fragments) < 2:
        return owners
    joined = "".join(text for text, _ in fragments)
    count = joined.count(query)
    return (
        [
            {
                "members": [info for _, info in fragments],
                "count": count,
                "expected_role": all(info["expected_role"] for _, info in fragments),
            }
        ]
        if count
        else []
    )


def anchor_result(ref, rows, visible, markdown, crops, criteria):
    """检查源文字的编辑所有者、实际图片所有者和跨格式可见性。"""
    owners = local_text_hits(ref, rows, visible, criteria)
    raster_copies = [
        {"index": crop["index"], "path": crop["path"], **comparison}
        for crop in crops
        if (comparison := witness_match(ref["witness"], crop, criteria))["passed"]
    ]
    public_text = sum(owner["count"] for owner in owners)
    seen_in_markdown = norm(ref["text"]) in markdown
    outside = public_text == 1 and seen_in_markdown
    if ref["role"] == "flex":
        passed = (outside and not raster_copies) or (public_text == 0 and len(raster_copies) == 1)
    else:
        passed = outside and not raster_copies
    return {
        "text": ref["text"],
        "role": ref["role"],
        "visible_at_source": public_text,
        "markdown_text": seen_in_markdown,
        "owners": owners,
        "expected_role": public_text == 1 and all(owner["expected_role"] for owner in owners),
        "expected_role_pass": passed and all(owner["expected_role"] for owner in owners),
        "raster_copies": raster_copies,
        "passed": passed,
    }


def score_page(sample, page, public_text, md, crops, criteria):
    """逐个输出按源定义评分，另记可比较的一对一边界命中。"""
    flat = public_text_blocks(page)
    groups = []
    primary = []
    for group in sample["groups"]:
        scope = envelope(group["parts"], criteria["scope_padding"])
        parts = [part_result(part, scope, crops, criteria) for part in group["parts"]]
        families = {entry["index"] for part in parts for entry in part["owners"]}
        groups.append(
            {
                "id": group["id"],
                "scope": scope,
                "owner_count": len(families),
                "representation": "composite" if len(families) == 1 else "separate-or-partial",
                "parts": parts,
                "passed": all(part["passed"] for part in parts),
            }
        )
        primary.extend(
            {"id": f"{group['id']}/{part['id']}", "bbox": part["box"]}
            for part in group["parts"]
            if part["role"] in {"primary", "panel"}
        )
    _, overlaps = pair_regions(primary, [crop for crop in crops if crop["linked"]])
    geometry = {part["id"]: round(overlaps.get(part["id"], 0), 6) for part in primary}
    return {
        "groups": groups,
        "geometry": geometry,
        "protected": [anchor_result(ref, flat, public_text, md, crops, criteria) for ref in sample["keep"]],
        "flex": [anchor_result(ref, flat, public_text, md, crops, criteria) for ref in sample["flex"]],
        "crops": [{k: v for k, v in crop.items() if k != "image"} for crop in crops],
    }


def score_document(source, folder, criteria, expected_runtime):
    """核对原件身份，并在整本文档成品上只计预先冻结的物理页。"""
    status = load(folder / "status.json")
    assert status["sha256"] == source["sha256"] and status["physical_pages"] == source["pages"], folder
    assert Path(status["source_root"]).resolve() == Path(expected_runtime["snapshot"]).resolve(), folder
    full = status["status"] == "complete"
    assert full or status["status"] == "failed", folder
    if full:
        pages = load(folder / "middle.json.gz")["pages"]
        html = (folder / "document.html").read_text()
        raw_markdown = (folder / "document.md").read_text()
        md, md_paths = markdown_representation(raw_markdown)
        # Markdown 的图片题名可以是 figcaption，仍属于可见图外说明。
        md_with_captions = norm(BeautifulSoup(re.sub(r"!\[[^\]]*\]\([^)]*\)", "", raw_markdown), "html.parser").get_text(" "))
        dom = main_dom(html)
        editable = html_text(html)
        assert len(pages) == source["pages"], folder
    else:
        pages, dom, editable, md, md_paths, md_with_captions = None, {}, {}, "", [], ""
    scored = {}
    for number, sample in source["annotated"].items():
        physical = int(number)
        page = pages[physical - 1] if full else {"blocks": [], "page_idx": physical - 1}
        page_dom = dom.get(physical, {})
        crops = visible_figures(page, page_dom, folder, md_paths)
        record = score_page(sample, page, editable.get(physical, []), md + md_with_captions, crops, criteria)
        record["parse"] = status["status"]
        scored[number] = record
    return scored


def summarise(documents, criteria):
    """分别延续图件完整性、物理墨迹重复及图外文字的真实分母。"""
    pages = [page for source in documents.values() for page in source.values()]
    groups = [group for page in pages for group in page["groups"]]
    parts = [part for group in groups for part in group["parts"]]
    ious = [value for page in pages for value in page["geometry"].values()]
    result = {
        "parsed_pages": [sum(page["parse"] == "complete" for page in pages), len(pages)],
        "complete_nonduplicating_groups": [sum(group["passed"] for group in groups), len(groups)],
        "primary_geometry_at_0.5": [sum(value >= criteria["geometry_iou_report"] for value in ious), len(ious)],
        "duplicate_physical_witnesses": sum(len(p["duplicate_witnesses"]) for p in parts),
    }
    for role in sorted({p["role"] for p in parts}):
        selected = [p for p in parts if p["role"] == role]
        result[f"{role}_ink_and_coverage"] = [sum(p["passed"] for p in selected), len(selected)]
    for role in ("protected", "flex"):
        selected = [anchor for page in pages for anchor in page[role]]
        result[f"{role}_text"] = [sum(a["passed"] for a in selected), len(selected)]
        result[f"{role}_text_and_role"] = [sum(a["expected_role_pass"] for a in selected), len(selected)]
        result[f"{role}_text_rasterized"] = sum(bool(a["raster_copies"]) for a in selected)
    return result


def independent_boundaries(sources, folder, criteria):
    """布局教师只看同一源图，单独核对预测框和原图部件几何。"""
    output = {}
    for engine, suffix in {"pp": "json.gz", "vlm": "json"}.items():
        rows = {}
        for slug, source in sources.items():
            for physical, page in source["annotated"].items():
                key = f"d-{source['sha256'][:16]}-p{physical}"
                file = folder / engine / f"{key}.{suffix}"
                if not file.is_file():
                    continue
                labels = load(file)
                assert labels["image_sha256"] == page["source_image_sha256"], file
                boxes = [
                    {"bbox": box.get("bbox_normalized", box["bbox"])}
                    for box in labels["boxes"]
                    if box.get("label", box.get("type")) in {"image", "chart", "seal"}
                ]
                # 与公开图体评分共用组内身份，避免同页多幅同名示波图互相覆盖。
                parts = [{**part, "id": f"{group['id']}/{part['id']}"} for group in page["groups"] for part in group["parts"]]
                primary = [{"id": part["id"], "bbox": part["box"]} for part in parts if part["role"] in {"primary", "panel"}]
                _, matches = pair_regions(primary, boxes)
                rows[f"{slug}/{physical}"] = {
                    "geometry": {x["id"]: round(matches.get(x["id"], 0.0), 6) for x in primary},
                    "legend_coverage": {
                        x["id"]: round(max((fraction(x["box"], y["bbox"]) for y in boxes), default=0.0), 6)
                        for x in parts
                        if x["role"] == "legend"
                    },
                }
        ious = [value for row in rows.values() for value in row["geometry"].values()]
        legends = [value for row in rows.values() for value in row["legend_coverage"].values()]
        output[engine] = {
            "pages": len(rows),
            "primary_geometry_at_0.5": [sum(iou >= criteria["geometry_iou_report"] for iou in ious), len(ious)],
            "legend_geometry_coverage": [sum(x >= criteria["part_coverage"] for x in legends), len(legends)],
            "documents": rows,
        }
    return output


def main():
    """从一个固定 GT 对输入各臂逐路打分，并保存跨整本输出比较。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gt", type=Path, required=True)
    parser.add_argument("--arm", action="append", required=True, metavar="NAME=DIR")
    parser.add_argument("--teachers", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    encoded_gt = args.gt.read_bytes()
    raw_gt = gzip.decompress(encoded_gt) if args.gt.suffix == ".gz" else encoded_gt
    gold = json.loads(raw_gt)
    gt_content_sha = sha256(raw_gt).hexdigest()
    assert gold["schema"] == "flash-source-visual-ownership-v1"
    sources = gold["sources"]
    criteria = gold["geometry_rules"]
    roots = defaultdict(list)
    for assignment in args.arm:
        label, path = assignment.split("=", 1)
        roots[label].append(Path(path).resolve())
    output = {"gt_sha256": sha256(encoded_gt).hexdigest(), "gt_content_sha256": gt_content_sha, "routes": {}}
    baseline = next(iter(roots))
    for route in ROUTES:
        arms = {}
        diffs = {}
        for label, paths in roots.items():
            folders = resolve_documents(paths, route, sources)
            runtime = load(paths[0] / "runtime.json")
            if runtime.get("gold_sha256") != gt_content_sha:
                raise ValueError(f"回放使用的源图真值与当前夹具不同 {paths[0]}")
            documents = {slug: score_document(sources[slug], folder, criteria, runtime) for slug, folder in folders.items()}
            arms[label] = {
                "summary": summarise(documents, criteria),
                "documents": documents,
                "runtime": runtime,
                "folders": {slug: str(folder) for slug, folder in folders.items()},
            }
            if label != baseline:
                diffs[label] = {
                    slug: changed_outputs(Path(arms[baseline]["folders"][slug]), folder) for slug, folder in folders.items()
                }
        output["routes"][route] = {"arms": arms, "delta_from_first_arm": diffs}
    if args.teachers:
        output["independent_label_boundaries"] = independent_boundaries(sources, args.teachers, criteria)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                route: {arm: data["summary"] for arm, data in result["arms"].items()}
                for route, result in output["routes"].items()
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
