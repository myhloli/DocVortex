"""在相同运行时交替比较两个冻结源码，保留逐文档耗时、公开输出指纹与进程树峰值内存。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time


def _worker(args: argparse.Namespace) -> None:
    """预热后计量公共 parse，内存包含当前进程及渲染子进程，输出指纹不进入解析计时。"""
    sys.path.insert(0, str(args.source_root.resolve() / "src"))
    import psutil
    from loguru import logger
    from docvortex.api import parse
    from docvortex._compute_backend import backend_info

    logger.remove()
    sources = sorted(args.input_dir.glob("*.pdf"))
    for index in sorted({0, len(sources) // 4, len(sources) // 2, 3 * len(sources) // 4, len(sources) - 1}):
        parse(sources[index])
    process = psutil.Process()
    stop = threading.Event()
    peak = [0]

    def sample() -> None:
        """用低频采样记录同一进程树的 RSS 总量，不累计已经退出的子进程峰值。"""
        while not stop.is_set():
            resident = 0
            for member in [process, *process.children(recursive=True)]:
                try:
                    resident += member.memory_info().rss
                except psutil.Error:
                    pass
            peak[0] = max(peak[0], resident)
            stop.wait(0.05)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    documents = []
    try:
        for source in sources:
            started = time.perf_counter()
            result = parse(source, keep_model_json=True)
            elapsed = time.perf_counter() - started
            model = json.dumps(result.model_json.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            documents.append(
                {"name": source.stem, "seconds": elapsed, "model_sha256": hashlib.sha256(model.encode()).hexdigest()}
            )
    finally:
        stop.set()
        sampler.join()
    args.output.write_text(
        json.dumps(
            {
                "seconds": sum(item["seconds"] for item in documents),
                "peak_tree_rss_bytes": peak[0],
                "backend": backend_info(),
                "python": sys.executable,
                "documents": documents,
            },
            indent=2,
        )
    )


def main() -> None:
    """五轮默认按 AB、BA 交替执行，检查同一源码的输出稳定性后汇总配对耗时与内存。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--candidate-root", type=Path)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if args.worker:
        _worker(args)
        return
    args.output.mkdir(parents=True, exist_ok=False)
    runs = {"before": [], "after": []}
    roots = {"before": args.baseline_root.resolve(), "after": args.candidate_root.resolve()}
    for round_index in range(args.rounds):
        for kind in ["before", "after"] if round_index % 2 == 0 else ["after", "before"]:
            result_path = args.output / f"{round_index + 1}-{kind}.json"
            env = {**os.environ, "PYTHONPATH": str(roots[kind] / "src")}
            subprocess.run(
                [
                    args.python,
                    str(Path(__file__).resolve()),
                    "--worker",
                    "--source-root",
                    str(roots[kind]),
                    "--input-dir",
                    str(args.input_dir.resolve()),
                    "--output",
                    str(result_path),
                ],
                env=env,
                check=True,
            )
            record = json.loads(result_path.read_text())
            runs[kind].append(record)
            print(
                round_index + 1, kind, round(record["seconds"], 3), round(record["peak_tree_rss_bytes"] / 2**20, 1), flush=True
            )
    for kind, records in runs.items():
        fingerprints = [{item["name"]: item["model_sha256"] for item in record["documents"]} for record in records]
        assert all(fingerprint == fingerprints[0] for fingerprint in fingerprints), kind
    report = {"rounds": args.rounds, "order": "AB/BA alternating", "warmup_documents_per_process": 5}
    for kind, records in runs.items():
        report[kind] = {
            "median_seconds": statistics.median(record["seconds"] for record in records),
            "median_peak_tree_rss_bytes": statistics.median(record["peak_tree_rss_bytes"] for record in records),
        }
    report["time_ratio"] = report["after"]["median_seconds"] / report["before"]["median_seconds"]
    report["rss_ratio"] = report["after"]["median_peak_tree_rss_bytes"] / report["before"]["median_peak_tree_rss_bytes"]
    (args.output / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
