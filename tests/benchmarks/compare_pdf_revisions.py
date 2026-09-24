"""按文档交替比较冻结版本和当前版本的两个后端，保存完整输出与进程树 RSS。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from rust_pdf import source_identity, write_json

ROOT = Path(__file__).resolve().parents[2]


def measure(path, folder, source, backend, args):
    """每次测量使用独立解释器，检查真实源码与输入；已完成记录可直接续跑。"""
    package = (source / "src/docvortex/__init__.py").resolve()
    if not (folder / "report.json").is_file():
        command = [
            sys.executable,
            str(ROOT / "tests/benchmarks/rust_pdf.py"),
            "--suite",
            args.suite,
            "--backend",
            backend,
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
    record = json.loads((folder / "report.json").read_text())["documents"][0]
    assert Path(record["source_package"]) == package
    assert record["compute"]["backend"] == backend
    assert record["source_code_sha256"] == source_identity(package)["source_code_sha256"]
    assert all(len(values) == args.runs for values in record["seconds"].values())
    return record


def compare(measurements):
    """输出摘要必须全等，分别计算同版本后端差异及跨版本时间和内存变化。"""
    assert len({item["source_sha256"] for item in measurements.values()}) == 1
    assert len({item["region_input_sha256"] for item in measurements.values()}) == 1
    results = {}
    for label, before, after in (
        ("backends", "python-current", "rust-current"),
        ("rust-revision", "rust-reference", "rust-current"),
        ("python-revision", "python-reference", "python-current"),
    ):
        old, new = measurements[before], measurements[after]
        results[label] = {
            "time_ratios": {
                stage: value / old["median_seconds"][stage] if old["median_seconds"][stage] else 1.0
                for stage, value in new["median_seconds"].items()
            },
            "rss_ratio": new["memory"]["sampled_tree_peak_rss_bytes"] / old["memory"]["sampled_tree_peak_rss_bytes"],
        }
    return {"equal": len({item["full_output_sha256"] for item in measurements.values()}) == 1, "ratios": results}


def main():
    """交替四个实现，超过退化门槛时反序复测；不覆盖产物或刷新输出金标。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference-source", type=Path, required=True)
    parser.add_argument("--suite", choices=("public", "shared"), default="public")
    parser.add_argument("--flash-baseline", type=Path)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--path", type=Path, action="append")
    parser.add_argument("--extra-path", type=Path, action="append", default=[])
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("runs must be positive")
    manifest = json.loads((ROOT / "tests/fixtures/flash_layout_geometry_manifest.json").read_text())
    paths = args.path or [
        *(ROOT / item["path"] for item in manifest["documents"]),
        *sorted((ROOT / "tests/unittest/pdfs/native_pdf_tables").glob("*.pdf")),
    ]
    paths = list(dict.fromkeys(path.resolve() for path in paths + args.extra_path))
    args.output.mkdir(parents=True, exist_ok=True)
    old = args.reference_source.resolve()
    variants = [
        ("python-reference", old, "python"),
        ("rust-reference", old, "rust"),
        ("python-current", ROOT, "python"),
        ("rust-current", ROOT, "rust"),
    ]
    report = {"suite": args.suite, "runs": args.runs, "documents": []}
    gate_stages = {"parse"} if args.suite == "public" else {"text_extraction", "text_total", "table_total"}
    for index, path in enumerate(paths):
        order = variants[index % 4 :] + variants[: index % 4]
        folder = args.output / f"{index:02d}-{path.stem}"
        measurements = {label: measure(path, folder / label, source, backend, args) for label, source, backend in order}
        record = {"path": str(path), "measurements": measurements, **compare(measurements)}
        repeat_pairs = [
            label
            for label, value in record["ratios"].items()
            if value["rss_ratio"] > 1.05
            or any(ratio > 1.05 for stage, ratio in value["time_ratios"].items() if stage in gate_stages)
        ]
        if repeat_pairs:
            pair_labels = {
                "backends": ("python-current", "rust-current"),
                "rust-revision": ("rust-reference", "rust-current"),
                "python-revision": ("python-reference", "python-current"),
            }
            required = {label for pair in repeat_pairs for label in pair_labels[pair]}
            repeated = {
                label: measure(path, folder / f"repeat-{label}", source, backend, args)
                for label, source, backend in reversed(order)
                if label in required
            }
            record["repeat_pairs"] = repeat_pairs
            record["repeat_measurements"] = repeated
            record["repeat_comparison"] = compare({**measurements, **repeated})
            record["persistent_regression"] = any(
                value["rss_ratio"] > 1.05
                and record["repeat_comparison"]["ratios"][label]["rss_ratio"] > 1.05
                or any(
                    ratio > 1.05 and record["repeat_comparison"]["ratios"][label]["time_ratios"][stage] > 1.05
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
