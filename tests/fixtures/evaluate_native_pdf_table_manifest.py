# Copyright (c) Opendatalab. All rights reserved.
"""Native Table release gate that runs external spread PDF true value manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from collections import defaultdict
from pathlib import Path, PureWindowsPath
from typing import Any

from docvortex.analyzers.native.pdf._table_recovery import (
    NativeTableInput,
    coerce_native_table_rectangles,
    coerce_native_table_rules,
    recover_native_pdf_table,
)
from docvortex.analyzers.native.pdf._table_recovery.engine import diagnose_native_pdf_table
from docvortex.document.pdf._document import PDFDocument

_DEFAULT_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "unittest" / "pdfs" / "native_pdf_tables"


def _validate_portable_manifest(manifest: dict[str, Any]) -> None:
    """Refuse to solidify the corpus root directory or the absolute path of the host into version manifest."""

    forbidden_root_keys = {"source_root", "source_root_hint"}
    persisted_root_keys = forbidden_root_keys.intersection(manifest)
    if persisted_root_keys:
        keys = ", ".join(sorted(persisted_root_keys))
        raise ValueError(f"manifest 不得保存语料根目录字段: {keys}")

    for collection_name in ("entries", "flash_targets"):
        for entry_index, entry in enumerate(
            manifest.get(collection_name, []),
        ):
            filename = str(entry["file"])
            path = Path(filename)
            if path.is_absolute() or PureWindowsPath(filename).is_absolute() or ".." in path.parts:
                raise ValueError(
                    f"manifest {collection_name}[{entry_index}].file 必须是 source root 下的安全相对路径: {filename}"
                )


def _result_signature(result: Any) -> dict[str, Any] | None:
    """Convert native structure results to a stable signature comparable to manifest."""

    if result is None:
        return None
    return {
        "rows": result.rows,
        "cols": result.cols,
        "span_signature": [
            [cell.row, cell.col, cell.rowspan, cell.colspan] for cell in result.cells if cell.rowspan > 1 or cell.colspan > 1
        ],
        "text_sha256": hashlib.sha256("\n".join(cell.content for cell in result.cells).encode("utf-8")).hexdigest(),
    }


def _recover_table_input(
    table_input: NativeTableInput,
    *,
    measure_performance: bool,
) -> tuple[dict[str, Any] | None, float]:
    """Press the performance door switch to perform a function recovery, or perform a formal timing recovery after warm-up."""

    if not measure_performance:
        return _result_signature(recover_native_pdf_table(table_input)), 0.0
    # Performance mode retains an untimed warm-up to avoid Python first path and page cache thrashing contamination of p95.
    recover_native_pdf_table(table_input)
    started = time.perf_counter()
    result = recover_native_pdf_table(table_input)
    return _result_signature(result), time.perf_counter() - started


def _maybe_diagnose_table_input(
    table_input: NativeTableInput,
    *,
    collect_diagnostics: bool,
    mismatch: bool,
) -> dict[str, Any] | None:
    """Expensive candidate diagnostics are only performed when a diagnostic is explicitly requested or when the results do not match."""

    if not collect_diagnostics and not mismatch:
        return None
    return diagnose_native_pdf_table(table_input)


def _evaluate_entry(
    document: PDFDocument,
    entry: dict[str, Any],
    page_cache: dict[int, tuple[Any, ...]],
    *,
    measure_performance: bool,
) -> tuple[dict[str, Any] | None, float, NativeTableInput]:
    """Constructs a manifest form input and returns the structure signature, elapsed time, and diagnostic input."""

    page_index = int(entry["page_index"])
    if page_index not in page_cache:
        page = document[page_index]
        page_cache[page_index] = (
            page.size,
            tuple(page.get_chars()),
            coerce_native_table_rules(page.get_drawing_lines()),
            coerce_native_table_rectangles(page.get_path_infos()),
        )
    page_size, chars, rules, rectangles = page_cache[page_index]
    if "bbox_points" in entry:
        point_bbox = tuple(float(value) for value in entry["bbox_points"])
    else:
        bbox = entry["bbox"]
        # layout bbox Use integerization PDFPage Dimension reduction; evaluation must reproduce the same production coordinate boundary,
        # Avoid the generation of ghost orbits at the outer edge of the sub-point level.
        production_page_size = (int(page_size[0]), int(page_size[1]))
        point_bbox = tuple(
            bbox[index] * (production_page_size[0] if index % 2 == 0 else production_page_size[1]) for index in range(4)
        )
    table_input = NativeTableInput(
        table_bbox=point_bbox,
        page_size=page_size,
        angle=0,
        chars=chars,
        drawing_lines=rules,
        rectangles=rectangles,
    )
    actual, duration = _recover_table_input(
        table_input,
        measure_performance=measure_performance,
    )
    return actual, duration, table_input


def _entry_mismatch(
    entry: dict[str, Any],
    actual: dict[str, Any] | None,
) -> tuple[str, bool]:
    """Compares the output pattern, topology, and cell text hash of a truth entry."""

    actual_output = "html" if actual is not None else "projection"
    expected_output = entry["expected_output"]
    mismatch = actual_output != expected_output
    if actual is not None and expected_output == "html":
        mismatch = mismatch or any(
            actual[field] != entry[field]
            for field in (
                "rows",
                "cols",
                "span_signature",
                "text_sha256",
            )
        )
    return actual_output, mismatch


def main() -> None:
    """Check HTML coverage, structural truth, text hashing and parsing performance."""

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-root",
        type=Path,
        default=_DEFAULT_SOURCE_ROOT,
        help="PDF 语料目录；默认使用仓库内 Native Table fixtures。",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("tests/fixtures/native_pdf_table_cross_page_manifest.json"),
    )
    parser.add_argument(
        "--diagnostics-output",
        type=Path,
        help="可选写出逐表候选和首个拒绝门诊断。",
    )
    parser.add_argument(
        "--skip-performance-gate",
        action="store_true",
        help="跳过易受共享 CI 负载影响的 p95 门，仅用于 pytest 功能回归。",
    )
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    _validate_portable_manifest(manifest)
    entries_by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in manifest["entries"]:
        entries_by_file[entry["file"]].append(entry)

    target_mismatches: list[dict[str, Any]] = []
    regression_mismatches: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    html_count = 0
    target_html_count = 0
    durations: list[float] = []
    measure_performance = not args.skip_performance_gate
    collect_diagnostics = args.diagnostics_output is not None
    for filename, entries in entries_by_file.items():
        with PDFDocument(str(args.source_root / filename)) as document:
            page_cache: dict[int, tuple[Any, ...]] = {}
            for entry in entries:
                actual, duration, table_input = _evaluate_entry(
                    document,
                    entry,
                    page_cache,
                    measure_performance=measure_performance,
                )
                durations.append(duration)
                actual_output, mismatch = _entry_mismatch(entry, actual)
                diagnostic = _maybe_diagnose_table_input(
                    table_input,
                    collect_diagnostics=collect_diagnostics,
                    mismatch=mismatch,
                )
                if diagnostic is not None:
                    diagnostics.append(
                        {
                            "scope": "corpus",
                            "file": filename,
                            "page_index": entry["page_index"],
                            "table_index": entry["table_index"],
                            "bbox": entry["bbox"],
                            "expected_output": entry["expected_output"],
                            **diagnostic,
                        }
                    )
                if actual_output == "html":
                    html_count += 1
                is_target = entry["review_status"] == "target_verified"
                if is_target and actual_output == "html":
                    target_html_count += 1
                if mismatch:
                    mismatch_record = {
                        "file": filename,
                        "page_index": entry["page_index"],
                        "table_index": entry["table_index"],
                        "review_status": entry["review_status"],
                        "expected_output": entry["expected_output"],
                        "actual": actual,
                    }
                    if diagnostic is not None:
                        mismatch_record["diagnostic"] = diagnostic
                    if is_target:
                        target_mismatches.append(mismatch_record)
                    else:
                        regression_mismatches.append(mismatch_record)

    flash_targets_by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in manifest.get("flash_targets", []):
        flash_targets_by_file[entry["file"]].append(entry)
    flash_target_html_count = 0
    flash_target_mismatches: list[dict[str, Any]] = []
    for filename, entries in flash_targets_by_file.items():
        with PDFDocument(str(args.source_root / filename)) as document:
            page_cache = {}
            for entry in entries:
                actual, duration, table_input = _evaluate_entry(
                    document,
                    entry,
                    page_cache,
                    measure_performance=measure_performance,
                )
                durations.append(duration)
                actual_output, mismatch = _entry_mismatch(entry, actual)
                diagnostic = _maybe_diagnose_table_input(
                    table_input,
                    collect_diagnostics=collect_diagnostics,
                    mismatch=mismatch,
                )
                if diagnostic is not None:
                    diagnostics.append(
                        {
                            "scope": "flash_target",
                            "file": filename,
                            "page_index": entry["page_index"],
                            "table_index": entry["table_index"],
                            "bbox_points": entry["bbox_points"],
                            "expected_output": entry["expected_output"],
                            **diagnostic,
                        }
                    )
                if actual_output == "html":
                    flash_target_html_count += 1
                if mismatch:
                    mismatch_record = {
                        "file": filename,
                        "page_index": entry["page_index"],
                        "table_index": entry["table_index"],
                        "expected_output": entry["expected_output"],
                        "actual": actual,
                    }
                    if diagnostic is not None:
                        mismatch_record["diagnostic"] = diagnostic
                    flash_target_mismatches.append(mismatch_record)

    total = len(manifest["entries"])
    target_total = sum(entry["review_status"] == "target_verified" for entry in manifest["entries"])
    flash_target_total = len(manifest.get("flash_targets", []))
    p95_milliseconds = 1000.0 * statistics.quantiles(durations, n=20)[18] if len(durations) >= 20 else 0.0
    summary = {
        "accuracy_scope": manifest["accuracy_scope"],
        "tables": total,
        "html": html_count,
        "coverage": html_count / total if total else 0.0,
        "target_tables": target_total,
        "target_html": target_html_count,
        "target_mismatches": len(target_mismatches),
        "target_precision": ((target_html_count - len(target_mismatches)) / target_html_count if target_html_count else 0.0),
        "regression_mismatches": len(regression_mismatches),
        "flash_targets": flash_target_total,
        "flash_target_html": flash_target_html_count,
        "flash_target_mismatches": len(flash_target_mismatches),
        "p95_milliseconds": p95_milliseconds,
    }
    result_payload = {
        "summary": summary,
        "target_mismatches": target_mismatches,
        "regression_mismatches": regression_mismatches,
        "flash_target_mismatches": flash_target_mismatches,
    }
    print(json.dumps(result_payload, ensure_ascii=False, indent=2))
    if args.diagnostics_output is not None:
        args.diagnostics_output.parent.mkdir(parents=True, exist_ok=True)
        args.diagnostics_output.write_text(
            json.dumps(
                {
                    "summary": summary,
                    "tables": diagnostics,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    if (
        html_count != manifest["expected_html"]
        or target_total != manifest["target_verified_tables"]
        or target_html_count != target_total
        or target_mismatches
        or regression_mismatches
        or flash_target_html_count != flash_target_total
        or flash_target_mismatches
        or (not args.skip_performance_gate and p95_milliseconds > manifest["performance_p95_limit_ms"])
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
