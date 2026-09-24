"""在独立进程内测量公开解析和共享 PDF 接口，严格比较完整输出及固定表格区域。"""

from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict
import gc
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]


def write_json(path: Path, value: object) -> None:
    """将计时之外的完整结果保存为可复核 JSON。"""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(value: object) -> str:
    """不忽略字段、文字或坐标地计算完整协议摘要。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def public_once(payload: bytes) -> tuple[dict, dict]:
    """测量完整公开 parse，结果序列化和素材摘要不计入解析耗时。"""
    from docvortex import parse

    started = time.perf_counter()
    result = parse(payload, file_suffix="pdf", keep_model_json=True)
    elapsed = time.perf_counter() - started
    return {"parse": elapsed}, {
        "model": result.model_json.model_dump(mode="json"),
        "middle": result.middle_json.model_dump(mode="json"),
        "assets": {key: hashlib.sha256(value).hexdigest() for key, value in result.assets.items()},
        "diagnostics": [asdict(item) for item in result.diagnostics],
    }


def shared_once(payload: bytes, model: list) -> tuple[dict, dict]:
    """以冻结的 Flash 区域输入分别测量提取、复用、文本物化和表格恢复。"""
    from docvortex.analyzers.pdf import apply_text_evidence, prepare_table_page, prepare_text_evidence, recover_table_region
    from docvortex.document.pdf import PDFDocument

    times = defaultdict(float)
    outputs = []
    with PDFDocument(payload) as document:
        if len(document) != len(model):
            raise ValueError("Frozen region page count differs")
        for index, blocks in enumerate(model):
            page = document[index]
            size = page.size
            tables = [
                (tuple(v * size[i % 2] for i, v in enumerate(b["bbox"])), int(b.get("angle", 0) or 0))
                for b in blocks
                if b["type"] == "table"
            ]
            started = time.perf_counter()
            geometry = page.get_chars_with_geometry()
            times["text_extraction"] += time.perf_counter() - started
            started = time.perf_counter()
            vectors = page.get_vector_geometry()
            times["vector_extraction"] += time.perf_counter() - started
            started = time.perf_counter()
            evidence = prepare_text_evidence(
                page, geometry=geometry, vector_geometry=vectors, table_regions=[b for b, _a in tables]
            )
            times["prepare_text_evidence"] += time.perf_counter() - started
            materialized = deepcopy(blocks)
            started = time.perf_counter()
            apply_text_evidence(materialized, evidence)
            times["apply_text_evidence"] += time.perf_counter() - started
            recovered = []
            if tables:
                started = time.perf_counter()
                prepared = prepare_table_page(page, geometry=geometry, vector_geometry=vectors)
                times["prepare_table_page"] += time.perf_counter() - started
                for bbox, angle in tables:
                    started = time.perf_counter()
                    result = recover_table_region(prepared, bbox, angle=angle)
                    times["recover_table_region"] += time.perf_counter() - started
                    recovered.append(asdict(result) if result is not None else None)
            outputs.append(
                {
                    "styles": [asdict(x) for x in evidence.styles],
                    "links": [asdict(x) for x in evidence.links],
                    "scripts": [asdict(x) for x in evidence.scripts],
                    "blocks": materialized,
                    "tables": recovered,
                }
            )
    times["text_total"] = times["prepare_text_evidence"] + times["apply_text_evidence"]
    times["table_total"] = times["prepare_table_page"] + times["recover_table_region"]
    return dict(times), {"pages": outputs}


def worker(args: argparse.Namespace) -> None:
    """先预热再计时，在计时外验证重复输出，并另跑一轮独立采样内存。"""
    from loguru import logger
    from docvortex._compute_backend import backend_info
    from docvortex.version import __version__
    import docvortex
    from pipeline import MemorySampler

    logger.disable("docvortex")
    path = Path(args.worker)
    payload = path.read_bytes()
    source_hash = hashlib.sha256(payload).hexdigest()
    model = None
    regions_hash = None
    if args.suite == "shared":
        baseline = json.loads((args.flash_baseline / "report.json").read_text())
        record = next((r for r in baseline["documents"] if r["source_sha256"] == source_hash), None)
        if record is None:
            raise ValueError("Source PDF is missing from the frozen Flash baseline")
        model = json.loads((args.flash_baseline / record["artifact"] / "output.json").read_text())["model_list"]
        regions_hash = digest(model)
    times = defaultdict(list)
    expected = None
    args.output.mkdir(parents=True, exist_ok=False)
    progress = {
        "path": str(path.resolve()),
        "source_sha256": source_hash,
        "source_package": str(Path(docvortex.__file__).resolve()),
        "compute": backend_info(),
        "status": "timing",
        "completed_runs": 0,
    }
    write_json(args.output / "progress.json", progress)
    for run in range(args.runs + 1):
        gc.collect()
        durations, output = public_once(payload) if args.suite == "public" else shared_once(payload, model)
        current = digest(output)
        if expected is not None and current != expected:
            raise AssertionError(f"Non-deterministic output: {path}")
        expected = current
        if run == 0:
            first = durations
        else:
            for stage, value in durations.items():
                times[stage].append(value)
        progress.update(completed_runs=run, seconds=dict(times), full_output_sha256=expected)
        write_json(args.output / "progress.json", progress)
    write_json(args.output / "output.json", output)
    del output
    gc.collect()
    progress["status"] = "memory"
    write_json(args.output / "progress.json", progress)
    sampler = MemorySampler()
    sampler.thread.start()
    try:
        memory_times, memory_output = public_once(payload) if args.suite == "public" else shared_once(payload, model)
    finally:
        memory = sampler.finish()
    assert digest(memory_output) == expected
    record = {
        "path": str(path.resolve()),
        "source_sha256": source_hash,
        "region_input_sha256": regions_hash,
        "full_output_sha256": expected,
        "first_seconds": first,
        "seconds": dict(times),
        "median_seconds": {key: statistics.median(values) for key, values in times.items()},
        "memory": memory,
        "source_version": __version__,
        "source_package": str(Path(docvortex.__file__).resolve()),
        "compute": backend_info(),
    }
    write_json(args.output / "result.json", record)
    progress["status"] = "complete"
    write_json(args.output / "progress.json", progress)
    print(path.name, record["median_seconds"], flush=True)


def main() -> None:
    """每份文档独立运行，拒绝覆盖报告或比较不同源文件/区域输入。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--flash-baseline", type=Path)
    parser.add_argument("--suite", choices=("public", "shared"), default="public")
    parser.add_argument("--backend", choices=("python", "rust", "auto"), required=True)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--path", type=Path, action="append")
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.runs < 1 or args.output.exists():
        parser.error("runs must be positive and output must not already exist")
    if args.suite == "shared" and args.flash_baseline is None:
        parser.error("shared benchmarks require --flash-baseline with frozen region inputs")
    os.environ["DOCVORTEX_COMPUTE_BACKEND"] = args.backend
    # 子进程渲染器另行初始化日志，使用环境变量避免 DEBUG I/O 混入正式计时。
    os.environ["LOGURU_LEVEL"] = "WARNING"
    if args.worker:
        worker(args)
        return
    paths = args.path or [ROOT / "demo/pdfs" / f"{name}.pdf" for name in ("caibao1", "demo1", "demo2")]
    args.output.mkdir(parents=True)
    records = []
    for index, path in enumerate(paths):
        folder = args.output / f"{index:02d}-{path.stem}"
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            str(path.resolve()),
            "--output",
            str(folder.resolve()),
            "--backend",
            args.backend,
            "--suite",
            args.suite,
            "--runs",
            str(args.runs),
        ]
        if args.flash_baseline:
            command += ["--flash-baseline", str(args.flash_baseline.resolve())]
        subprocess.run(command, cwd=ROOT, check=True)
        record = json.loads((folder / "result.json").read_text())
        record["artifact"] = folder.name
        records.append(record)
    report = {
        "suite": args.suite,
        "backend": args.backend,
        "python": sys.version,
        "platform": platform.platform(),
        "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "git_status": subprocess.check_output(["git", "status", "--short"], cwd=ROOT, text=True),
        "dependencies": {name: version(name) for name in ("docvortex", "pypdfium2", "pydantic", "numpy")},
        "documents": records,
    }
    if args.baseline:
        old = json.loads((args.baseline / "report.json").read_text())
        if old["suite"] != args.suite or len(old["documents"]) != len(records):
            raise AssertionError("Different benchmark suite or corpus")
        comparisons = []
        for previous, current in zip(old["documents"], records, strict=True):
            if (previous["source_sha256"], previous["region_input_sha256"]) != (
                current["source_sha256"],
                current["region_input_sha256"],
            ):
                raise AssertionError("Different source or frozen region input")
            comparisons.append(
                {
                    "path": current["path"],
                    "equal": previous["full_output_sha256"] == current["full_output_sha256"],
                    "time_ratios": {
                        stage: value / previous["median_seconds"][stage] if previous["median_seconds"][stage] else None
                        for stage, value in current["median_seconds"].items()
                    },
                }
            )
        report["comparisons"] = comparisons
    write_json(args.output / "report.json", report)
    if any(not item["equal"] for item in report.get("comparisons", [])):
        raise SystemExit("Full output differs; inspect saved output.json")


if __name__ == "__main__":
    main()
