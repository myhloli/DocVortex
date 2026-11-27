"""在可移植源图真值上比较公开图体边界，保留漏图及解析失败的分母。"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

if __package__:
    from .score_flash_layout_gt import flat_blocks, inner_text, norm, pair_regions
else:
    from score_flash_layout_gt import flat_blocks, inner_text, norm, pair_regions

DEFAULT_GT = Path(__file__).resolve().parents[1] / "tests/fixtures/flash_layout_gt_r14_graphic_regions_v1.json.gz"


def public_page_evidence(roots: list[Path], sha: str, physical: int) -> tuple[list[dict], str, bool]:
    """核对物理页和公开资产，读取真实图体和未归入图片的独立文字。"""
    for root in roots:
        for document_id in (sha, f"d-{sha[:16]}"):
            folder = root / "documents" / document_id
            file = folder / "status.json"
            if not file.is_file():
                continue
            status = json.loads(file.read_bytes())
            if status.get("sha256") != sha:
                raise ValueError(f"源文件哈希不符 {file}")
            if status["status"] == "failed":
                return [], "", False
            if status["status"] != "complete":
                raise ValueError(f"回放尚未结束 {file}")
            with gzip.open(folder / "middle.json.gz", "rt", encoding="utf-8") as handle:
                page = json.load(handle)["pages"][physical - 1]
            if page["page_idx"] + 1 != physical:
                raise ValueError(f"物理页不符 {sha} {physical}")
            bodies = []
            for block in page["blocks"]:
                if block["type"] not in {"image", "chart"}:
                    continue
                captions = [norm(inner_text(c.get("content"))) for c in block["content"] if c["type"].endswith("_caption")]
                for child in block["content"]:
                    if child["type"].endswith("_body") and child.get("image_path") and (folder / child["image_path"]).is_file():
                        bodies.append({"bbox": child["bbox"], "captions": captions})
            text_blocks = (b["text"] for b in flat_blocks(page) if b["type"] not in {"image", "chart", "equation"})
            return bodies, " ".join(text_blocks), True
    raise FileNotFoundError(f"缺少文档 {sha} 的整本公开输出")


def covers_repeated_raster(expected: list[float], actual: list[float]) -> bool:
    """单一真实图片须容纳至少九成源图，且源图占此裁片至少一半，避免整页背景虚高命中。"""
    overlap = max(0, min(expected[2], actual[2]) - max(expected[0], actual[0])) * max(
        0, min(expected[3], actual[3]) - max(expected[1], actual[1])
    )
    source_area = (expected[2] - expected[0]) * (expected[3] - expected[1])
    crop_area = (actual[2] - actual[0]) * (actual[3] - actual[1])
    return source_area > 0 and crop_area > 0 and overlap >= 0.9 * source_area and overlap >= 0.5 * crop_area


def main() -> None:
    """分别统计图体边界和独立文字探针，重复出现的短锚可从逐页记录复核。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gt", type=Path)
    parser.add_argument("--suite", choices=["public", "opportunity", "transfer", "repeated"], required=True)
    parser.add_argument("--baseline-dir", type=Path, action="append", required=True)
    parser.add_argument("--candidate-dir", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    default_gt = (
        DEFAULT_GT.with_name("flash_layout_gt_r15_repeated_rasters_v1.json.gz") if args.suite == "repeated" else DEFAULT_GT
    )
    raw = (args.gt or default_gt).read_bytes()
    fixture = json.loads(gzip.decompress(raw))
    rows = []
    totals = {}
    for side, roots in (("A", args.baseline_dir), ("C", args.candidate_dir)):
        hits = count = ok_pages = 0
        anchor_count = anchors_found = anchors_unique = 0
        iou_sum = 0.0
        foreground_hits = caption_hits = caption_total = 0
        for sample in fixture["pages"]:
            if sample["suite"] != args.suite:
                continue
            regions = sample["boundary_regions"]
            bodies, text, complete = public_page_evidence(roots, sample["pdf_sha256"], sample["physical_page"])
            ok_pages += int(complete)
            pairs, overlaps = pair_regions(regions, bodies)
            per_region = {r["id"]: overlaps.get(r["id"], 0.0) for r in regions}
            hits += sum(v >= 0.5 for v in per_region.values())
            iou_sum += sum(per_region.values())
            count += len(regions)
            mask = sample["mask"]
            anchors = mask.get("retained", [mask["protected_text"]] if "protected_text" in mask else [])
            occurrences = {anchor: text.count(norm(anchor)) for anchor in anchors}
            anchor_count += len(occurrences)
            anchors_found += sum(value > 0 for value in occurrences.values())
            anchors_unique += sum(value == 1 for value in occurrences.values())
            repeat_checks = {}
            if args.suite == "repeated":
                for region in regions:
                    ident = region["id"]
                    body = bodies[pairs[ident]] if ident in pairs else None
                    visible = bool(body and covers_repeated_raster(region["bbox"], body["bbox"]))
                    foreground_hits += int(visible)
                    check = {"covered": visible}
                    if "caption" in region:
                        correct = bool(visible and body["captions"] == [norm(region["caption"])])
                        caption_total += 1
                        caption_hits += int(correct)
                        check["unique_expected_caption"] = correct
                    repeat_checks[ident] = check
            rows.append(
                {
                    "side": side,
                    "sha256": sample["pdf_sha256"],
                    "page": sample["physical_page"],
                    "iou": per_region,
                    "text_anchor_occurrences": occurrences,
                    **({"repeat_checks": repeat_checks} if repeat_checks else {}),
                }
            )
        totals[side] = {
            "ok_pages": ok_pages,
            "regions": count,
            "hits_at_0.5": hits,
            "mean_matched_iou": iou_sum / count if count else None,
            "text_anchors": anchor_count,
            "text_anchors_found": anchors_found,
            "text_anchors_unique": anchors_unique,
        }
        if args.suite == "repeated":
            totals[side].update(
                covered_foregrounds=foreground_hits, expected_captions=caption_total, unique_expected_captions=caption_hits
            )
    if not rows:
        parser.error(f"GT 中没有 {args.suite} 样本")
    report = {"gt_sha256": hashlib.sha256(raw).hexdigest(), "suite": args.suite, "summary": totals, "rows": rows}
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)


if __name__ == "__main__":
    main()
