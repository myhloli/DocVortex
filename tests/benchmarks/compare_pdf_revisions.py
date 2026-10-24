"""按文档交替比较冻结版本和当前版本的原生实现，保存完整输出与进程树 RSS。"""

from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys

from rust_pdf import digest, write_json
from pdf_corpus import corpus_manifest, corpus_paths
from pdf_memory import NATIVE_PAIRS, benchmark_identity, peak_rss, validate_identity, validate_measurements

ROOT = Path(__file__).resolve().parents[2]


def validate_timings(record: dict, runs: int) -> None:
    """核对实际计时次数和阶段完整性，空计时或不同采样配置不得恢复复用。"""
    seconds = record.get("seconds")
    medians = record.get("median_seconds")
    required = {"parse"} if record["suite"] == "public" else {"text_extraction", "text_total", "table_total"}
    if not seconds or not medians or set(seconds) != set(medians) or not required <= set(seconds):
        raise ValueError("Benchmark timing stages are missing or inconsistent")
    for stage, values in seconds.items():
        if not isinstance(values, list) or len(values) != runs:
            raise ValueError(f"Benchmark timing runs mismatch: {stage}")
        for value in [*values, medians[stage]]:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"Benchmark timing must be finite and nonnegative: {stage}")


def measure(path, folder, source, args):
    """每次测量使用独立解释器，检查真实源码与输入；已完成记录可直接续跑。"""
    package = (source / "src/docvortex/__init__.py").resolve()
    if not (folder / "report.json").is_file():
        command = [
            sys.executable,
            str(ROOT / "tests/benchmarks/rust_pdf.py"),
            "--suite",
            args.suite,
            "--runs",
            str(args.runs),
            "--path",
            str(path),
            "--output",
            str(folder),
        ]
        if args.flash_baseline:
            command += ["--flash-baseline", str(args.flash_baseline.resolve())]
        subprocess.run(command, cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(package.parents[1])), check=True)
    report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    if report.get("suite") != args.suite:
        raise ValueError("Benchmark identity mismatch: suite")
    if len(report.get("documents", [])) != 1:
        raise ValueError("Single-document benchmark report must contain exactly one document")
    record = {**report["documents"][0], "suite": report["suite"]}
    expected = benchmark_identity(path, package, args.suite, args.flash_baseline)
    validate_identity(record, expected)
    validate_timings(record, args.runs)
    artifact = record.get("artifact")
    if not isinstance(artifact, str) or not artifact:
        raise ValueError("Benchmark report is missing its complete output artifact")
    output_path = (folder / artifact / "output.json").resolve()
    if not output_path.is_relative_to(folder.resolve()):
        raise ValueError("Benchmark complete output artifact escapes its report directory")
    output = json.loads(output_path.read_text(encoding="utf-8"))
    if digest(output) != record.get("full_output_sha256"):
        raise ValueError("Benchmark complete output artifact differs from its recorded fingerprint")
    if record.get("memory_method") != "isolated-entry-after-one-warmup":
        # 已完成的耗时与输出不重写；用同一源码单独审计 RSS，并保留原校验进程的内存值。
        from pdf_memory import measure as measure_memory

        memory = measure_memory(
            {
                **record,
                "suite": args.suite,
                "flash_baseline": str(args.flash_baseline.resolve()) if args.flash_baseline else None,
            },
            folder / "memory-audit",
        )
        record["validation_inclusive_memory"] = record["memory"]
        record["memory"] = memory["memory"]
        record["memory_method"] = memory["method"]
        record["memory_audit_artifact"] = str(folder / "memory-audit/result.json")
    peak_rss(record)
    return record


def compare(measurements):
    """输出摘要必须全等，计算跨版本时间和内存变化。"""
    validate_measurements(measurements)
    records = list(measurements.values())
    for field in ("source_sha256", "region_input_sha256", "page_count", "suite", "environment"):
        if any(field not in item for item in records) or any(item[field] != records[0][field] for item in records[1:]):
            raise ValueError(f"Benchmark comparison inputs differ: {field}")
    if any(not item.get("full_output_sha256") for item in measurements.values()):
        raise ValueError("Benchmark comparison requires complete output fingerprints")
    results = {}
    for label, (before, after) in NATIVE_PAIRS.items():
        old, new = measurements[before], measurements[after]
        if not old["median_seconds"] or set(old["median_seconds"]) != set(new["median_seconds"]):
            raise ValueError("Benchmark timing stages differ between native versions")
        results[label] = {
            "time_ratios": {
                stage: value / old["median_seconds"][stage] if old["median_seconds"][stage] else 1.0 if value == 0 else None
                for stage, value in new["median_seconds"].items()
            },
            "zero_stage_new_work": [
                stage for stage, value in new["median_seconds"].items() if old["median_seconds"][stage] == 0 and value > 0
            ],
            "rss_ratio": peak_rss(new) / peak_rss(old),
        }
    return {"equal": len({item["full_output_sha256"] for item in measurements.values()}) == 1, "ratios": results}


def main():
    """交替两个原生版本，超过退化门槛时反序复测；不覆盖产物或刷新输出金标。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference-source", type=Path, required=True)
    parser.add_argument("--candidate-source", type=Path, default=ROOT, help="冻结候选源码，隔离同工作区的其他任务修改")
    parser.add_argument("--suite", choices=("public", "shared"), default="public")
    parser.add_argument("--flash-baseline", type=Path)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--path", type=Path, action="append")
    parser.add_argument("--extra-path", type=Path, action="append", default=[])
    parser.add_argument("--corpus", choices=("demo", "all"), default="all")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("runs must be positive")
    if args.suite == "shared" and args.flash_baseline is None:
        parser.error("shared benchmarks require --flash-baseline")
    paths = args.path or corpus_paths(args.corpus)
    paths = list(dict.fromkeys(path.resolve() for path in paths + args.extra_path))
    if not paths:
        parser.error("benchmark corpus must not be empty")
    args.output.mkdir(parents=True, exist_ok=True)
    inputs = corpus_manifest(paths)
    corpus_file = args.output / "corpus.json"
    if corpus_file.exists() and json.loads(corpus_file.read_text(encoding="utf-8")) != inputs:
        raise ValueError("Existing benchmark output has a different input corpus")
    report_file = args.output / "report.json"
    if report_file.exists():
        previous = json.loads(report_file.read_text(encoding="utf-8"))
        if previous.get("suite") != args.suite or previous.get("runs") != args.runs:
            raise ValueError("Existing benchmark output has different suite or timing runs")
    write_json(args.output / "corpus.json", inputs)
    old = args.reference_source.resolve()
    variants = [
        ("rust-reference", old),
        ("rust-current", args.candidate_source.resolve()),
    ]
    report = {
        "suite": args.suite,
        "runs": args.runs,
        "corpus": args.corpus if not args.path else "explicit",
        "corpus_manifest": inputs,
        "python": sys.version,
        "platform": platform.platform(),
        "dependencies": {name: version(name) for name in ("docvortex", "pypdfium2", "pydantic", "numpy")},
        "documents": [],
    }
    gate_stages = {"parse"} if args.suite == "public" else {"text_extraction", "text_total", "table_total"}
    for index, path in enumerate(paths):
        if hashlib.sha256(path.read_bytes()).hexdigest() != inputs[index]["sha256"]:
            raise AssertionError(f"PDF input changed during benchmark: {path}")
        order = variants[index % 2 :] + variants[: index % 2]
        folder = args.output / f"{index:02d}-{path.stem}"
        measurements = {label: measure(path, folder / label, source, args) for label, source in order}
        record = {
            "path": str(path),
            "pages": next(iter(measurements.values()))["page_count"],
            "measurements": measurements,
            **compare(measurements),
        }
        repeat_pairs = [
            label
            for label, value in record["ratios"].items()
            if value["rss_ratio"] > 1.05
            or any(ratio is None or ratio > 1.05 for stage, ratio in value["time_ratios"].items() if stage in gate_stages)
        ]
        if repeat_pairs:
            required = {label for pair in repeat_pairs for label in NATIVE_PAIRS[pair]}
            repeated = {
                label: measure(path, folder / f"repeat-{label}", source, args)
                for label, source in reversed(order)
                if label in required
            }
            record["repeat_pairs"] = repeat_pairs
            record["repeat_measurements"] = repeated
            record["repeat_comparison"] = compare({**measurements, **repeated})
            record["persistent_regression"] = any(
                value["rss_ratio"] > 1.05
                and record["repeat_comparison"]["ratios"][label]["rss_ratio"] > 1.05
                or any(
                    (ratio is None or ratio > 1.05)
                    and (
                        record["repeat_comparison"]["ratios"][label]["time_ratios"][stage] is None
                        or record["repeat_comparison"]["ratios"][label]["time_ratios"][stage] > 1.05
                    )
                    for stage, ratio in value["time_ratios"].items()
                    if stage in gate_stages
                )
                for label, value in record["ratios"].items()
                if label in repeat_pairs
            )
        report["documents"].append(record)
        write_json(args.output / "progress.json", report)
        print(
            f"{index + 1}/{len(paths)} {path.name}: equal={record['equal']} regression={record.get('persistent_regression', False)}",
            flush=True,
        )
    write_json(args.output / "report.json", report)
    if any(
        not item["equal"]
        or not item.get("repeat_comparison", {"equal": True})["equal"]
        or item.get("persistent_regression", False)
        for item in report["documents"]
    ):
        raise SystemExit("Output difference or persistent regression; inspect report.json")


if __name__ == "__main__":
    main()
