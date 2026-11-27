"""按同一份独立表格 GT 比较两个公开 MiddleJson 全文重放。"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
from statistics import fmean
import unicodedata

import numpy as np

from docvortex.render._internal.common.html_table import HtmlTableError, parse_html_tables

if __package__:
    from .score_flash_layout_gt import flat_blocks, geom, pair_regions, read_replay
    from .verify_flash_layout_replication_gt import DEFAULT_GT, ReplicationGold, read_json
else:
    from score_flash_layout_gt import flat_blocks, geom, pair_regions, read_replay
    from verify_flash_layout_replication_gt import DEFAULT_GT, ReplicationGold, read_json


def table_matches(regions: list[dict], predictions: list[dict]) -> dict:
    """同一对象的可接受外框取最大 IoU，优先最多的有效一对一匹配，再比较几何精度。"""
    if not regions or not predictions:
        return {}
    try:
        from scipy.optimize import linear_sum_assignment
    except ImportError as error:
        raise RuntimeError("运行一对一布局匹配需安装 scipy") from error
    overlaps = np.array(
        [
            [
                max(geom(box, pred["bbox"]) for box in [region["bbox"], *region.get("accepted_alternative_bboxes", [])])
                for pred in predictions
            ]
            for region in regions
        ]
    )
    # 一个达标匹配的奖励高于全部匹配的小数 IoU 之和，避免贪心导致少认一张真表。
    weights = np.where(overlaps >= 0.5, 1 + min(overlaps.shape) + overlaps, 0.0)
    rows, cols = linear_sum_assignment(-weights)
    return {regions[i]["id"]: (int(j), float(overlaps[i, j])) for i, j in zip(rows, cols, strict=True) if weights[i, j] > 0}


def cell_text(text: str) -> str:
    """消除字宽、空白和等价负号差异，保留数字、小数点、分隔符、单位及其他标点。"""
    return "".join(unicodedata.normalize("NFKC", text).casefold().split()).replace("−", "-")


def inspect_rows(grid, assertions: list) -> list[dict]:
    """要求列值同时出现在一行的各个独立单元格，避免在多行或一个跨列格内拼出命中。"""
    if not assertions:
        return []
    columns = {member["column_0based"] for group in assertions for member in group}
    positions = {}
    for cell in grid.cells if grid else ():
        value = cell_text(cell.tag.get_text())
        for col in columns:
            if cell.column <= col <= cell.end_column:
                for row in range(cell.row, cell.end_row + 1):
                    positions[row, col] = ((cell.row, cell.column), value)
    results = []
    for group in assertions:
        wanted = [(m["column_0based"], cell_text(m["text"])) for m in group]
        matches = []
        for row in range(grid.row_count if grid else 0):
            owners = {
                positions[row, col][0] for col, text in wanted if (row, col) in positions and positions[row, col][1] == text
            }
            if len(owners) == len(wanted):
                matches.append(row)
        results.append({"members": group, "matching_rows": matches, "pass": bool(matches)})
    return results


def inspect_table(markup: str, target: dict) -> dict:
    """依据有界逻辑网格核查局部行列及完整格值；缺失、畸形或多个顶层表不获结构分。"""
    try:
        grids = parse_html_tables(markup) if markup else ()
    except HtmlTableError:
        grids = ()
    grid = grids[0] if len(grids) == 1 else None
    shape = target.get("table_dimensions") or {}
    dimensions = {}
    for axis, attribute in (("rows", "row_count"), ("columns", "column_count")):
        if shape.get(axis) is not None:
            actual = getattr(grid, attribute, None)
            dimensions[axis] = {"expected": shape[axis], "actual": actual, "pass": actual == shape[axis]}
    cells = []
    for assertion in target.get("cell_assertions", []):
        row = assertion.get("row_0based")
        if assertion.get("row_from_end") is not None:
            row = (grid.row_count if grid else 0) - assertion["row_from_end"]
        col = assertion["column_0based"]
        candidates = []
        anchor = None
        for cell in grid.cells if grid else ():
            if cell.column <= col <= cell.end_column and (row is None or cell.row <= row <= cell.end_row):
                candidates.append(cell.tag.get_text(separator="", strip=False))
                if (cell.row, cell.column) == (row, col):
                    anchor = cell
        wanted = cell_text(assertion["text"])
        result = {**assertion, "actual": candidates, "pass": any(cell_text(value) == wanted for value in candidates)}
        if assertion.get("rowspan") is not None:
            result["actual_span"] = [anchor.rowspan, anchor.colspan] if anchor is not None else None
            result["span_pass"] = result["pass"] and result["actual_span"] == [assertion["rowspan"], assertion["colspan"]]
        cells.append(result)
    return {"dimensions": dimensions, "cells": cells, "rows": inspect_rows(grid, target.get("row_assertions", []))}


def coverage(region, table) -> float:
    """按被保护区域自身面积计算覆盖率，巨表包含小区域时也能检出。"""
    left, top, right, bottom = region
    width = max(0, min(right, table[2]) - max(left, table[0]))
    height = max(0, min(bottom, table[3]) - max(top, table[1]))
    return width * height / ((right - left) * (bottom - top))


def score_page(page: dict, middle: dict) -> dict:
    """逐页评估局部表对象、可编辑格值、显式关系和非表保护；只在穷尽标注页统计额外表。"""
    blocks = flat_blocks(middle)
    tables = [b for b in blocks if b["type"] == "table"]
    other = [b for b in blocks if b["type"] != "table"]
    regions = {r["id"]: r for r in page["regions"]}
    targets = [r for r in regions.values() if r["type"] == "table" and "bbox" in r["scored_fields"]]
    matches = table_matches(targets, tables)
    others, _ = pair_regions([r for r in regions.values() if r["type"] != "table" and r["bbox"]], other)
    # 阅读顺序关系使用真实公开块编号；几何匹配以页内数组位置保持一对一。
    mapped = {rid: tables[j] for rid, (j, _) in matches.items()}
    mapped.update({rid: other[j] for rid, j in others.items()})
    assessments = []
    for region in targets:
        rid = region["id"]
        index, iou = matches.get(rid, (None, 0.0))
        pred = tables[index] if index is not None else {}
        assessments.append(
            {
                "id": rid,
                "matched": index is not None,
                "prediction_index": pred.get("index"),
                "iou": iou,
                **inspect_table(pred.get("html", ""), region),
            }
        )

    checked = {}
    for rid in page.get("protected_non_tables", []):
        checked[rid] = all(coverage(regions[rid]["bbox"], table["bbox"]) < 0.75 for table in tables)
    combinations = [
        not any(
            all(
                coverage(regions[rid]["bbox"], table["bbox"]) >= group["min_coverage_per_region"]
                for rid in group["covered_regions"]
            )
            for table in tables
        )
        for group in page.get("forbidden_table_combinations", [])
    ]
    relations = []
    for rel in page.get("relations", []):
        if rel["kind"] == "outside_bodies":
            for rid in rel["regions"]:
                for bid in rel["bodies"]:
                    separate = all(coverage(regions[rid]["bbox"], table["bbox"]) < 0.75 for table in tables)
                    relations.append(
                        {"kind": rel["kind"], "ids": [rid, bid], "pass": rid in mapped and bid in mapped and separate}
                    )
        elif rel["kind"] == "distinct_objects":
            ids = rel["regions"]
            relations.append({"kind": rel["kind"], "ids": ids, "pass": all(rid in mapped for rid in ids)})
        elif rel["kind"] == "before":
            for left, right in rel["pairs"]:
                ai, bi = mapped.get(left, {}).get("index"), mapped.get(right, {}).get("index")
                relations.append(
                    {"kind": "before", "ids": [left, right], "pass": ai is not None and bi is not None and ai < bi}
                )
        elif rel["kind"] in {"caption_of", "footnote_of"}:
            for child, parent in rel["pairs"]:
                a, b = mapped.get(child, {}), mapped.get(parent, {})
                relations.append(
                    {
                        "kind": rel["kind"],
                        "ids": [child, parent],
                        "pass": a.get("parent") is not None and a["parent"] == b.get("index"),
                    }
                )
    exhaustive = "table" in page["exhaustive_types"]
    metrics = {
        "table_recall": [r["matched"] for r in assessments],
        "table_iou": [r["iou"] for r in assessments],
        "row_count": [r["dimensions"]["rows"]["pass"] for r in assessments if "rows" in r["dimensions"]],
        "column_count": [r["dimensions"]["columns"]["pass"] for r in assessments if "columns" in r["dimensions"]],
        "cell_value": [c["pass"] for r in assessments for c in r["cells"]],
        "cell_span": [c["span_pass"] for r in assessments for c in r["cells"] if "span_pass" in c],
        "same_row": [row["pass"] for r in assessments for row in r["rows"]],
        "protected_non_table": list(checked.values()),
        "protected_combination": combinations,
        "relations": [r["pass"] for r in relations],
        "exhaustive_page_inventory": [len(tables) == len(matches) == len(targets)] if exhaustive else [],
        "negative_page": [len(tables) == 0] if exhaustive and page.get("visual_table_count") == 0 else [],
    }
    return {
        "table_count": len(tables),
        "extra_tables": len(tables) - len(matches) if exhaustive else None,
        "tables": assessments,
        "protected_non_tables": checked,
        "forbidden_combinations_pass": combinations,
        "relations": relations,
        "metrics": metrics,
    }


def aggregate(records: list[dict]) -> dict:
    """分别汇总样本分母与家族宏平均，不把未标字段计零，不用大表的格数压过小家族。"""
    output = {}
    for name in records[0]["A"]["metrics"] if records else ():
        result = {}
        for tag in ("A", "D"):
            families = defaultdict(lambda: defaultdict(list))
            total = count = 0
            for row in records:
                values = row[tag]["metrics"][name]
                if values:
                    families[row["family_id"]][row["document_id"]].append(fmean(values))
                    total += sum(values)
                    count += len(values)
            family_scores = [fmean(fmean(pages) for pages in docs.values()) for docs in families.values()]
            result[tag] = {
                "count": count,
                "sum": total,
                "families": len(families),
                "family_macro": fmean(family_scores) if family_scores else None,
            }
        output[name] = result
    return output


def compare(gold: ReplicationGold, baseline_dirs: list[Path], candidate_dirs: list[Path]) -> dict:
    """按实际源摘要和物理页定位完整 A/D，输出逐页证据与按机制分组的配对结果。"""
    groups = defaultdict(list)
    for page in gold.pages:
        groups[page.document_id].append(page.model_dump(mode="json", exclude_unset=True))
    records = []
    for pages in groups.values():
        a, d = (read_replay(roots, pages[0]) for roots in (baseline_dirs, candidate_dirs))
        for p in pages:
            idx = p["physical_page"] - 1
            record = {field: p[field] for field in ("document_id", "family_id", "stratum", "physical_page")}
            record["A"] = score_page(p, a[idx])
            record["D"] = record["A"] if a[idx] == d[idx] else score_page(p, d[idx])
            records.append(record)
    strata = sorted({p["stratum"] for p in records})
    return {
        "gt_sha256": gold.annotation_sha256,
        "scope": "独立复制集专项。单表取一对一 IoU≥0.5，允许等价框。非表保护阈值为被覆盖面积的75%。",
        "by_stratum": {key: aggregate([row for row in records if row["stratum"] == key]) for key in strata},
        "summary": aggregate(records),
        "pages": records,
    }


def main() -> None:
    """从命令行读取可移植夹具及两个版本的完整公开重放目录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gt", type=Path, default=DEFAULT_GT)
    parser.add_argument("--baseline-dir", type=Path, action="extend", nargs="+", required=True)
    parser.add_argument("--candidate-dir", type=Path, action="extend", nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    opts = parser.parse_args()
    gold = ReplicationGold.model_validate_json(read_json(opts.gt))
    output = opts.output.resolve()
    if output == opts.gt.resolve() or any(
        output.is_relative_to((root / "documents").resolve()) for root in (*opts.baseline_dir, *opts.candidate_dir)
    ):
        parser.error("评分输出不能覆盖输入 GT 或完整重放文档")
    result = compare(gold, opts.baseline_dir, opts.candidate_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
