"""重放局部窄网格标注，分别评价数据表的可编辑性和符号图的视觉保留。"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path

from docvortex.render._internal.common.html_table import HtmlTableError, parse_html_tables

if __package__:
    from .score_flash_layout_gt import flat_blocks, pair_regions, read_replay
    from .verify_flash_layout_gt import read_json
else:
    from score_flash_layout_gt import flat_blocks, pair_regions, read_replay
    from verify_flash_layout_gt import read_json

DEFAULT_GT = Path(__file__).resolve().parents[1] / "tests/fixtures/flash_layout_narrow_grid_v1.json.gz"


def score_regions(regions: list[dict], page: dict) -> list[dict]:
    """同页对表、图分别做一对一匹配；只有几何达标且 HTML 含有效单元格的表可算可编辑。"""
    outputs = flat_blocks(page)
    scores = {region["id"]: {} for region in regions}
    for kind in ("table", "image"):
        blocks = [b for b in outputs if b["type"] == kind]
        matched, overlaps = pair_regions(regions, blocks)
        for rid, row in scores.items():
            row[kind + "_iou"] = overlaps.get(rid, 0.0)
            if kind == "table":
                row["editable"] = False
                if row["table_iou"] >= 0.5:
                    html = blocks[matched[rid]]["html"]
                    try:
                        tables = parse_html_tables(html) if html else ()
                    except HtmlTableError:
                        tables = ()
                    row["editable"] = len(tables) == 1 and bool(tables[0].cells)
    return [scores[r["id"]] for r in regions]


def compare(pages: list[dict], baseline: list[Path], candidate: list[Path]) -> dict:
    """按来源实验与局部类别保留分母，同一文档多处真表的独立文档数单列。"""
    summary = defaultdict(dict)
    rows = []
    for page in pages:
        index = page["physical_page"] - 1
        regions = page["regions"]
        before, after = (read_replay(roots, page)[index] for roots in (baseline, candidate))
        a = score_regions(regions, before)
        d = score_regions(regions, after) if before != after else a
        for target, left, right in zip(regions, a, d, strict=True):
            group = summary[page["cohort"]].setdefault(target["kind"], {"regions": 0, "documents": set()})
            group["regions"] += 1
            group["documents"].add(page["document_id"])
            for name, result in (("A", left), ("D", right)):
                counts = group.setdefault(name, {"editables": 0, "tables": 0, "images": 0})
                counts["editables"] += result["editable"]
                counts["tables"] += result["table_iou"] >= 0.5
                counts["images"] += result["image_iou"] >= 0.5
            rows.append(
                {
                    "cohort": page["cohort"],
                    "document_id": page["document_id"],
                    "physical_page": index + 1,
                    "id": target["id"],
                    "kind": target["kind"],
                    "A": left,
                    "D": right,
                }
            )
    for groups in summary.values():
        for item in groups.values():
            item["documents"] = len(item["documents"])
    return {"summary": summary, "rows": rows}


def main() -> None:
    """使用含完整原件输出的重放根目录计算冻结窄网格挑战成绩。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gt", type=Path, default=DEFAULT_GT)
    parser.add_argument("--baseline-dir", type=Path, nargs="+", required=True)
    parser.add_argument("--candidate-dir", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dest = args.output.resolve()
    if dest == args.gt.resolve() or any(
        dest.is_relative_to((d / "documents").resolve()) for d in args.baseline_dir + args.candidate_dir
    ):
        parser.error("输出路径与输入重叠")
    result = compare(json.loads(read_json(args.gt))["pages"], args.baseline_dir, args.candidate_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
