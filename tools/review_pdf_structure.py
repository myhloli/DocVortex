"""在隔离进程中比较结构解析开关，保存完整输出、布局标框和配对性能。"""

from __future__ import annotations

import argparse
import gc
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def measure(source: Path, output: Path, mode: str, runs: int, formats: list[str]) -> dict:
    """完成预热和独立计时，产物与阶段计时均来自实际公共解析入口。"""
    from loguru import logger
    from docvortex.api import parse
    from docvortex.analyzers.native.pdf import tagged
    from docvortex.document.pdf import PDFDocument
    from docvortex.render import PdfLayout, PdfRenderOptions
    from docvortex.visualization import render_layout_pdf

    logger.remove()
    data = source.read_bytes()
    times, phase_runs = [], []
    result = None
    for repeat in range(runs + 1):
        result = None
        gc.collect()
        phases = {}

        def timed(name, function):
            """为单次测量包装现有入口，不修改生产接口或记录非确定性协议字段。"""

            def invoke(*args, **kwargs):
                """累计实际执行时间，失败调用也保留对应阶段开销。"""
                started = time.perf_counter()
                try:
                    return function(*args, **kwargs)
                finally:
                    phases[name] = phases.get(name, 0.0) + time.perf_counter() - started

            return invoke

        with (
            patch.object(PDFDocument, "_get_structure", timed("structure_seconds", PDFDocument._get_structure)),
            patch.object(tagged, "read_marked_page", timed("content_seconds", tagged.read_marked_page)),
            patch.object(tagged, "_ordered_merge", timed("ordering_seconds", tagged._ordered_merge)),
        ):
            started = time.perf_counter()
            result = parse(data, file_suffix="pdf", parse_mode="txt", pdf_structure=mode, keep_model_json=True)
            seconds = time.perf_counter() - started
        if repeat or runs == 0:
            times.append(seconds)
            phase_runs.append(phases)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    output.mkdir(parents=True, exist_ok=True)
    artifact_directory = output / mode
    result.save_bundle(artifact_directory, overwrite=True)
    (artifact_directory / "layout.pdf").write_bytes(render_layout_pdf(data, result.middle_json.pages))
    extensions = {"markdown": "md", "structured_content": "json", "latex": "tex"}
    for target in formats:
        options = PdfRenderOptions(layout=PdfLayout.REFLOW) if target == "pdf" else None
        result.export(
            artifact_directory / f"document.{extensions.get(target, target)}",
            output_format=target,
            options=options,
            overwrite=True,
        )
    report = {
        "source": str(source.resolve()),
        "sha256": hashlib.sha256(data).hexdigest(),
        "mode": mode,
        "pages": len(result.middle_json.pages),
        "seconds": times,
        "median_seconds": statistics.median(times),
        "peak_rss_bytes": peak if sys.platform == "darwin" else peak * 1024,
        "memory_scope": "isolated-parse-process-before-export",
        "artifact_directory": str(artifact_directory.resolve()),
        "phases": phase_runs,
        "diagnostics": [asdict(item) for item in result.diagnostics],
        "structure_nodes": len(result.middle_json.extensions.get("docvortex_pdf_structure", {}).get("nodes", {})),
    }
    (output / f"{mode}-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    """依次启动隔离进程并汇总每份原件的两种路径，失败以明确状态记录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--formats", default="markdown,html,structured_content,pdf,docx,epub,latex")
    parser.add_argument("--worker", choices=("auto", "off"))
    args = parser.parse_args()
    if args.runs < 0:
        parser.error("--runs must be zero or greater")
    if args.worker:
        measure(args.source[0], args.output, args.worker, args.runs, args.formats.split(","))
        return
    args.output.mkdir(parents=True, exist_ok=True)
    reports = []
    for source in args.source:
        folder = args.output / source.stem
        folder.mkdir(parents=True, exist_ok=True)
        for mode in ("off", "auto"):
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--source",
                str(source.resolve()),
                "--output",
                str(folder.resolve()),
                "--runs",
                str(args.runs),
                "--formats",
                args.formats,
                "--worker",
                mode,
            ]
            with (folder / f"{mode}.log").open("w", encoding="utf-8") as log:
                completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False)
            report = (
                json.loads((folder / f"{mode}-report.json").read_text())
                if completed.returncode == 0
                else {"source": str(source), "mode": mode, "error": completed.returncode}
            )
            reports.append(report)
            print(source.name, mode, report.get("median_seconds", report.get("error")), flush=True)
        (args.output / "report.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    if any("error" in report for report in reports):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
