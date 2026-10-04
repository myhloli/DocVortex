# ruff: noqa: E402
"""Generate Flash PDF full output baseline and measure elapsed time and peak memory in independent processes."""

from __future__ import annotations

import argparse
import cProfile
import gc
import hashlib
import importlib.metadata
import json
import os
import platform
import pstats
import statistics
import subprocess
import sys
import time
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

os.environ["ORT_DISABLE_TELEMETRY"] = "1"
import onnxruntime

# The baseline process turns off background telemetry that is not relevant to the PDF calculation, and the production package's run settings remain unchanged.
onnxruntime.disable_telemetry_events()

from docvortex.schema import Producer

ROOT = Path(__file__).resolve().parents[2]
from pdf_corpus import corpus_paths

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "unittest"))

from _flash_pdf_test_utils import _page_bbox_fingerprint, _page_fingerprint

from docvortex.analyzers.native import PdfModel
from docvortex.document.pdf import initialize_pdfium_runtime
from docvortex.document.pdf._document import PDFDocument
from docvortex.postprocess.document import model_json_to_middle_json
from docvortex.schema import ModelJson


def _read_pdf(path: Path) -> bytes:
    """Read the original corpus and decode the versioned XOR test file in memory."""
    payload = path.read_bytes()
    if path.suffix == ".xor":
        key = b"MinerU flash layout fixture"
        return bytes(value ^ key[index % len(key)] for index, value in enumerate(payload))
    return payload


def _path_label(path: Path) -> str:
    """Generate stable labels for corpus inside and outside the warehouse to avoid absolute path failure relative_to."""
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(ROOT.resolve()))
    except ValueError:
        return str(resolved)


def _digest(value: Any) -> str:
    """Computes a stable summary on the full JSON value, not ignoring geometry, spaces, or any fields."""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    """Writes the auditable JSON product, the parent directory created independently by this run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _predict(payload: bytes) -> list[list[dict[str, Any]]]:
    """Perform native analysis during a single document lifecycle, ensuring timings include page opens and closes."""
    with PDFDocument(payload) as document:
        return PdfModel().predict(document)


def _predict_with_timings(payload: bytes) -> tuple[list[list[dict[str, Any]]], dict[str, float]]:
    """The time consumption of the two main stages of form detection is recorded without changing the production interface."""
    from unittest.mock import patch

    from docvortex.analyzers.native.pdf import pipeline, table_detection, table_rules

    timings = {"detect_table_candidates_seconds": 0.0, "build_rule_table_candidates_seconds": 0.0}
    original_build = table_rules._build_rule_table_candidates
    original_detect = table_detection._detect_table_candidates

    def timed_build(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return original_build(*args, **kwargs)
        finally:
            timings["build_rule_table_candidates_seconds"] += time.perf_counter() - started

    def timed_detect(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return original_detect(*args, **kwargs)
        finally:
            timings["detect_table_candidates_seconds"] += time.perf_counter() - started

    with (
        patch.object(table_rules, "_build_rule_table_candidates", timed_build),
        patch.object(
            table_detection,
            "_build_rule_table_candidates",
            timed_build,
        ),
        patch.object(table_detection, "_detect_table_candidates", timed_detect),
        patch.object(
            pipeline,
            "_detect_table_candidates",
            timed_detect,
        ),
    ):
        pages = _predict(payload)
    return pages, timings


def _worker(path: Path, destination: Path, runs: int, profile: bool) -> None:
    """Isolate timing, full output, and process peak memory for one document to prevent other documents from contaminating RSS."""
    import resource

    from loguru import logger

    logger.disable("docvortex")
    payload = _read_pdf(path)
    first_seconds = None
    if runs:
        started = time.perf_counter()
        _predict(payload)
        first_seconds = time.perf_counter() - started
    durations = []
    expected_digest = None
    for _ in range(max(1, runs)):
        gc.collect()
        started = time.perf_counter()
        pages = _predict(payload)
        durations.append(time.perf_counter() - started)
        digest = _digest(pages)
        if expected_digest is not None and digest != expected_digest:
            raise AssertionError(f"Non-deterministic model-list: {path}")
        expected_digest = digest
        del pages
        _write_json(
            destination / "progress.json",
            {
                "path": str(path.resolve()),
                "source_sha256": hashlib.sha256(payload).hexdigest(),
                "status": "timing",
                "first_seconds": first_seconds,
                "seconds": durations,
                "completed_runs": len(durations),
                "model_list_sha256": expected_digest,
            },
        )
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    _write_json(
        destination / "progress.json",
        {
            "path": str(path.resolve()),
            "source_sha256": hashlib.sha256(payload).hexdigest(),
            "status": "stage_diagnostics",
            "first_seconds": first_seconds,
            "seconds": durations,
            "completed_runs": len(durations),
            "model_list_sha256": expected_digest,
        },
    )
    pages, stage_timings = _predict_with_timings(payload)
    middle = model_json_to_middle_json(
        ModelJson(
            pages=deepcopy(pages),
            page_index_map=[],
            metadata={"file_suffix": "pdf", "producer": Producer(name="docvortex", version="refactor-baseline")},
        ),
    ).model_dump(mode="json")
    output = {"model_list": pages, "middle_json": middle}
    _write_json(destination / "output.json", output)
    result = {
        "path": _path_label(path),
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "runtime": asdict(initialize_pdfium_runtime()),
        "pages": len(pages),
        "full_output_sha256": _digest(output),
        "page_fingerprints": [_page_fingerprint(page) for page in pages],
        "bbox_fingerprints": [_page_bbox_fingerprint(page) for page in pages],
        "seconds": durations,
        "first_seconds": first_seconds,
        "onnxruntime_telemetry": "disabled_before_import",
        "median_seconds": statistics.median(durations),
        "peak_rss_bytes": peak_rss * (1024 if sys.platform != "darwin" else 1),
        **stage_timings,
    }
    del pages, middle, output
    if profile:
        import docvortex

        package_root = Path(docvortex.__file__).resolve().parent
        profiler = cProfile.Profile()
        profiler.runcall(_predict, payload)
        stats = pstats.Stats(profiler)
        result["profile"] = [
            {
                "file": "docvortex/" + Path(filename).resolve().relative_to(package_root).as_posix(),
                "line": line,
                "function": name,
                "calls": values[1],
                "self_seconds": values[2],
                "cumulative_seconds": values[3],
            }
            for (filename, line, name), values in stats.stats.items()
            if "/docvortex/analyzers/native/pdf/" in filename or "/docvortex/document/pdf/" in filename
        ]
    from docvortex._compute_backend import backend_info
    from docvortex.version import __version__

    result["compute"] = backend_info()
    result["source_version"] = __version__
    _write_json(destination / "result.json", result)


def _compare(output: Path, baseline: Path, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Check the complete corpus collection, source file fingerprints and outputs, and report performance changes individually."""
    previous = json.loads((baseline / "report.json").read_text(encoding="utf-8"))
    by_path = {record["path"]: record for record in previous["documents"]}
    if set(by_path) != {record["path"] for record in results}:
        raise AssertionError("Baseline and candidate corpus differ")
    comparisons = []
    for record in results:
        old = by_path[record["path"]]
        if old["source_sha256"] != record["source_sha256"]:
            raise AssertionError(f"Input changed: {record['path']}")
        comparisons.append(
            {
                "path": record["path"],
                "equal": old["full_output_sha256"] == record["full_output_sha256"],
                "time_ratio": record["median_seconds"] / old["median_seconds"],
                "rss_ratio": record["peak_rss_bytes"] / old["peak_rss_bytes"],
            }
        )
    _write_json(output / "comparison.json", comparisons)
    return comparisons


def main() -> None:
    """Perform a full regression or a specified sample benchmark; launch a separate subprocess for each document."""
    if "--pipeline" in sys.argv:
        from pipeline import main as pipeline_main

        sys.argv.remove("--pipeline")
        pipeline_main()
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--runs", type=int, default=5, help="预热一次后计时次数；0 只运行功能校验")
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--corpus", choices=("demo", "all"), default="all")
    parser.add_argument("--profile", action="store_true", help="额外运行剖析；不计入耗时或 RSS 指标")
    parser.add_argument(
        "--backend", choices=("auto", "python", "rust"), default=os.environ.get("DOCVORTEX_COMPUTE_BACKEND", "auto")
    )
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.environ["DOCVORTEX_COMPUTE_BACKEND"] = args.backend
    if args.runs < 0:
        parser.error("--runs must be non-negative")
    if args.worker:
        _worker(args.worker, args.output, args.runs, args.profile)
        return
    if (args.output / "report.json").exists():
        parser.error("output already contains a report; choose a new directory")
    manifest = json.loads((ROOT / "tests/fixtures/flash_layout_geometry_manifest.json").read_text(encoding="utf-8"))
    paths = args.path or corpus_paths(args.corpus)
    results = []
    for index, relative in enumerate(dict.fromkeys(paths)):
        destination = args.output.resolve() / f"{index:02d}-{Path(relative).stem}"
        input_path = Path(relative).expanduser()
        if not input_path.is_absolute():
            input_path = ROOT / input_path
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            str(input_path),
            "--output",
            str(destination),
            "--runs",
            str(args.runs),
        ]
        if args.profile:
            command.append("--profile")
        subprocess.run(command, cwd=ROOT, check=True)
        record = json.loads((destination / "result.json").read_text(encoding="utf-8"))
        record["artifact"] = str(destination.relative_to(args.output.resolve()))
        results.append(record)
        print(f"{index + 1}/{len(paths)} {relative}: {record['median_seconds']:.3f}s", flush=True)
    historical = {item["path"]: item for item in manifest["documents"]}
    history_differences = []
    for record in results:
        old = historical.get(record["path"])
        if old is not None:
            expected = [page["fingerprint"] for page in old["pages"]]
            expected_bbox = [page["bbox_fingerprint"] for page in old["pages"]]
            if expected != record["page_fingerprints"] or expected_bbox != record["bbox_fingerprints"]:
                history_differences.append(record["path"])
    report = {
        "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        if (ROOT / ".git").exists()
        else None,
        "git_status": subprocess.check_output(["git", "status", "--short"], cwd=ROOT, text=True)
        if (ROOT / ".git").exists()
        else None,
        "python": sys.version,
        "platform": platform.platform(),
        "dependencies": {name: importlib.metadata.version(name) for name in ("docvortex", "pypdfium2", "numpy", "pydantic")},
        "runs": args.runs,
        "requested_backend": args.backend,
        "historical_baseline_sha": manifest["baseline_git_sha"],
        "historical_differences": history_differences,
        "documents": results,
    }
    _write_json(args.output / "report.json", report)
    if args.baseline and any(not item["equal"] for item in _compare(args.output, args.baseline, results)):
        raise SystemExit("Full output differs; inspect output.json artifacts and comparison.json")


if __name__ == "__main__":
    main()
