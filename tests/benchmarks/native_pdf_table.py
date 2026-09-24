"""对两组真实表格分别测量结构与样式，并比较完整结果及候选诊断。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from loguru import logger

from docvortex.analyzers.native.pdf._table_recovery import (
    NativeTableInput,
    coerce_native_table_rectangles,
    coerce_native_table_rules,
    recover_native_pdf_table,
)
from docvortex.analyzers.native.pdf._table_recovery.engine import diagnose_native_pdf_table
from docvortex.analyzers.native.pdf.table_text_styles import render_native_table_html_with_scripts
from docvortex.document.pdf import PDFDocument

ROOT = Path(__file__).resolve().parents[2]


def _cases(group: str) -> list[tuple[dict, NativeTableInput, dict, dict]]:
    """沿各 manifest 原有坐标约定读取字符几何，页面原语只提取一次。"""
    demo = group == "demo"
    manifest = json.loads(
        (ROOT / "tests/fixtures" / f"native_pdf_table_{'demo' if demo else 'cross_page'}_manifest.json").read_text()
    )
    entries = manifest["tables"] if demo else manifest["entries"] + manifest.get("flash_targets", [])
    source_root = ROOT / ("demo/pdfs" if demo else "tests/unittest/pdfs/native_pdf_tables")
    by_file = defaultdict(list)
    for entry in entries:
        by_file[entry["file"]].append(entry)
    cases = []
    for filename, targets in by_file.items():
        path = source_root / filename
        source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        with PDFDocument(str(path)) as document:
            pages = {}
            for entry in targets:
                page_index = entry["page_index"]
                if page_index not in pages:
                    page = document[page_index]
                    pages[page_index] = (
                        page.size,
                        page.get_chars_with_geometry(),
                        coerce_native_table_rules(page.get_drawing_lines()),
                        coerce_native_table_rectangles(page.get_path_infos()),
                    )
                size, geometry, rules, rectangles = pages[page_index]
                scale = size if demo else tuple(int(value) for value in size)
                bbox = (
                    tuple(entry["bbox_points"])
                    if "bbox_points" in entry
                    else tuple(value * scale[index % 2] for index, value in enumerate(entry["bbox"]))
                )
                table = NativeTableInput(bbox, size, int(entry.get("angle", 0)), tuple(geometry.chars), rules, rectangles)
                identity = {
                    "file": filename,
                    "page_index": page_index,
                    "table_index": entry["table_index"],
                    "bbox": bbox,
                    "angle": table.angle,
                    "source_sha256": source_hash,
                }
                cases.append((identity, table, geometry.tight_bboxes, geometry.origins))
    return cases


def _run_group(group: str, runs: int) -> tuple[dict, list[dict]]:
    """预热后分阶段计时，完整诊断与序列化均在计时之外执行。"""
    cases = _cases(group)
    core_times = [[] for _ in cases]
    style_times = [[] for _ in cases]
    records = []
    for run in range(runs + 1):
        for index, (identity, table, tight, origins) in enumerate(cases):
            started = time.perf_counter()
            result = recover_native_pdf_table(table)
            core_duration = time.perf_counter() - started
            started = time.perf_counter()
            html = render_native_table_html_with_scripts(result, table, tight, origins) if result is not None else None
            style_duration = time.perf_counter() - started
            if run:
                core_times[index].append(core_duration)
                style_times[index].append(style_duration)
            if run == runs:
                records.append(
                    {
                        "identity": identity,
                        "result": asdict(result) if result is not None else None,
                        "styled_html": html,
                        "diagnosis": diagnose_native_pdf_table(table),
                    }
                )
    core_medians = [statistics.median(values) for values in core_times]
    style_medians = [statistics.median(values) for values in style_times]
    caibao_styles = [value for value, case in zip(style_medians, cases, strict=True) if case[0]["file"] == "caibao1.pdf"]
    return {
        "tables": len(cases),
        "core_seconds": sum(core_medians),
        "core_p95_ms": statistics.quantiles(core_medians, n=20)[18] * 1000,
        "style_seconds": sum(style_medians),
        "caibao_style_seconds": sum(caibao_styles),
        "per_table_core_seconds": core_times,
        "per_table_style_seconds": style_times,
    }, records


def main() -> None:
    """写出可重放比较的完整基线，差异存在时返回失败。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument(
        "--backend", choices=("auto", "python", "rust"), default=os.environ.get("DOCVORTEX_COMPUTE_BACKEND", "auto")
    )
    args = parser.parse_args()
    os.environ["DOCVORTEX_COMPUTE_BACKEND"] = args.backend
    if args.runs < 1 or args.output.exists():
        parser.error("runs must be positive and output must not already exist")
    logger.disable("docvortex")
    report = {"python": sys.version, "platform": platform.platform(), "runs": args.runs, "groups": {}}
    outputs = {}
    for group in ("cross_page", "demo"):
        summary, records = _run_group(group, args.runs)
        report["groups"][group] = summary
        outputs[group] = records
        print(group, {key: value for key, value in summary.items() if not key.startswith("per_table")}, flush=True)
    args.output.mkdir(parents=True)
    payload = json.dumps(outputs, ensure_ascii=False, sort_keys=True)
    (args.output / "output.json").write_text(payload, encoding="utf-8")
    report["output_sha256"] = hashlib.sha256(payload.encode()).hexdigest()
    from docvortex._compute_backend import backend_info
    from docvortex.version import __version__

    report["compute"] = backend_info()
    report["source_version"] = __version__
    if args.baseline:
        previous = json.loads((args.baseline / "output.json").read_text())
        current = json.loads(payload)
        report["equal"] = previous == current
        report["different_tables"] = [
            {"group": group, "identity": new["identity"]}
            for group in outputs
            for old, new in zip(previous[group], current[group], strict=True)
            if old != new
        ]
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if report.get("equal") is False:
        raise SystemExit("Full native table output or diagnostics differ")


if __name__ == "__main__":
    main()
