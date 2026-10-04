"""Measuring the public pipeline in independent processes and freezing seven rendering results and full semantic artifacts."""

from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
import cProfile
from datetime import datetime, timezone
from functools import wraps
import gc
import hashlib
from importlib.metadata import version
from io import BytesIO
import json
import multiprocessing
import os
from pathlib import Path
import platform
import pstats
import re
import statistics
import subprocess
import sys
import threading
import time
from unittest.mock import patch
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
_CASE_CACHE: dict[str, tuple[object, dict[str, object], str]] = {}


def write_json(path: Path, value: object) -> None:
    """Write a complete auditable product, report using UTF-8 encoding."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(value: bytes) -> str:
    """Computes stable byte digests, preserving all differences in text, whitespace, and geometry."""
    return hashlib.sha256(value).hexdigest()


def memory_bytes(pids: list[int]) -> dict[int, int]:
    """Read the current RSS of each process; the sum of RSS includes shared pages and is not equivalent to unique physical memory."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            """Description Windows Fixed ABI for PROCESS_MEMORY_COUNTERS."""

            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in ("peak", "working", "paged_peak", "paged", "nonpaged_peak", "nonpaged", "pagefile", "pagefile_peak")
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        values = {}
        for pid in pids:
            handle = kernel.OpenProcess(0x410, False, pid)
            if handle:
                try:
                    counters = Counters()
                    counters.cb = ctypes.sizeof(counters)
                    if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                        values[pid] = counters.working
                finally:
                    kernel.CloseHandle(handle)
        return values
    result = subprocess.run(
        ["ps", "-o", "pid=,rss=", "-p", ",".join(map(str, pids))], capture_output=True, text=True, check=False
    )
    return {int(pid): int(rss) * 1024 for pid, rss in (line.split() for line in result.stdout.splitlines())}


class MemorySampler:
    """Periodically samples the parent process and its cropping child process, and analyzes the operation without participating in memory peaks."""

    def __init__(self) -> None:
        """Establish sampling status for the current single document process."""
        self.stop = threading.Event()
        self.peaks: dict[int, int] = {}
        self.total_peak = 0
        self.samples = 0
        self.thread = threading.Thread(target=self.collect, daemon=True)

    def collect(self) -> None:
        """Read the surviving cropping process and parent process memory every 50ms."""
        while not self.stop.is_set():
            pids = [os.getpid(), *(child.pid for child in multiprocessing.active_children() if child.pid)]
            values = memory_bytes(pids)
            for pid, value in values.items():
                self.peaks[pid] = max(value, self.peaks.get(pid, 0))
            self.total_peak = max(self.total_peak, sum(values.values()))
            self.samples += 1
            self.stop.wait(0.05)

    def finish(self) -> dict[str, object]:
        """Stops sampling and reports parent process peaks, child process peaks, and simultaneous RSS total peaks separately."""
        self.stop.set()
        self.thread.join()
        return {
            "parent_peak_rss_bytes": self.peaks.get(os.getpid(), 0),
            "children_peak_rss_bytes": {str(pid): value for pid, value in self.peaks.items() if pid != os.getpid()},
            "sampled_tree_peak_rss_bytes": self.total_peak,
            "sample_count": self.samples,
            "sample_interval_seconds": 0.05,
        }


def instrument(stack: ExitStack, timings: dict[str, float]) -> None:
    """Wrap the existing stage entry, record the time consumption of the included sub-stages, and do not change the call results."""
    from docvortex import api
    from docvortex.analyzers.native.models import PdfModel
    from docvortex.document.pdf import images
    from docvortex.export import files
    from docvortex.postprocess import document

    def wrap(owner: object, name: str, label: str) -> None:
        """Record phase accumulation time via a recoverable wrapper."""
        original = getattr(owner, name)

        @wraps(original)
        def timed(*args: object, **kwargs: object) -> object:
            """The exception path also records the time taken to maintain the original exception propagation."""
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                timings[label] = timings.get(label, 0.0) + time.perf_counter() - started

        stack.enter_context(patch.object(owner, name, timed))

    for owner, name, label in (
        (api, "prepare_source", "prepare_source"),
        (PdfModel, "predict", "native_pdf"),
        (images, "load_images_from_pdf_bytes_range", "raster"),
        (images, "_load_visual_crops_from_pdf_bytes_range", "raster"),
        (document, "model_json_to_middle_json", "postprocess_core"),
        (files, "materialize_middle", "materialize"),
    ):
        wrap(owner, name, label)


def source_case(name: str) -> tuple[object, dict[str, object], str]:
    """Read real corpus or the existing fifteen format fixture, and provide a deterministic long connection link."""
    from docvortex.schema import MiddleJson, PageInfo, TextBlock, TextSpan

    if name in _CASE_CACHE:
        return _CASE_CACHE[name]
    if name == "synthetic:continuations":
        middle = MiddleJson(
            pages=[
                PageInfo(
                    page_idx=0,
                    blocks=[
                        TextBlock(
                            type="text", index=i, content=[TextSpan(type="text", content="测试连续文本")], continues_prev=i > 0
                        )
                        for i in range(1000)
                    ],
                )
            ],
            metadata={"file_suffix": "html", "producer": {"name": "docvortex", "version": "0.2.0"}},
            is_full_document=True,
        )
        return middle, {}, digest(middle.to_json().encode())
    if name.startswith("fixture:"):
        sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "tests/unittest")]
        from test_format_matrix import source_payload

        suffix = name.split(":", 1)[1]
        data = source_payload(suffix)
        return data, {"file_suffix": suffix}, digest(data)
    path = ROOT / name
    data = path.read_bytes()
    if path.suffix == ".xor":
        key = b"MinerU flash layout fixture"
        return bytes(value ^ key[i % len(key)] for i, value in enumerate(data)), {"file_suffix": "pdf"}, digest(data)
    return path, {}, digest(data)


def artifact_signature(target: str, payload: bytes) -> object:
    """Unpack comparison for ZIP and page-by-page text and pixel comparison for PDF, excluding container time metadata."""
    if target in {"docx", "epub"}:
        with ZipFile(BytesIO(payload)) as package:
            return {name: digest(package.read(name)) for name in sorted(package.namelist())}
    if target == "pdf":
        import pypdfium2 as pdfium
        from docvortex.document.pdf import initialize_pdfium_runtime

        initialize_pdfium_runtime()
        signatures = []
        with pdfium.PdfDocument(payload) as document:
            for page in document:
                try:
                    with closing(page.get_textpage()) as textpage:
                        text = textpage.get_text_bounded()
                    bitmap = page.render(scale=1)
                    try:
                        image = bitmap.to_pil()
                        signatures.append({"size": image.size, "text": text, "pixels": digest(image.tobytes())})
                    finally:
                        bitmap.close()
                finally:
                    page.close()
        return signatures
    return digest(payload)


def pipeline_once(name: str, destination: Path | None = None) -> tuple[dict[str, float], dict[str, object]]:
    """Execute the public pipeline once; only capture the running results and write out the results, no difference calculation is performed for formal timing."""
    from docvortex import api
    from docvortex.result import DocumentResult
    from docvortex.schema import MiddleJson
    from docvortex.render.contracts import EpubRenderOptions, RenderFormat

    source, options, source_hash = source_case(name)
    timings: dict[str, float] = {}
    with ExitStack() as stack:
        instrument(stack, timings)
        started = time.perf_counter()
        if isinstance(source, MiddleJson):
            result = DocumentResult(source)
        else:
            analysis = api.analyze(source, **options)
            timings["analyze"] = time.perf_counter() - started
            post_start = time.perf_counter()
            result = api.postprocess(analysis, keep_model_json=True)
            timings["postprocess"] = time.perf_counter() - post_start
        timings["parse"] = time.perf_counter() - started
        artifacts = {}
        for target in RenderFormat:
            render_options = (
                EpubRenderOptions(modified_at=datetime(2000, 1, 1, tzinfo=timezone.utc)) if target.value == "epub" else None
            )
            render_start = time.perf_counter()
            try:
                artifacts[target.value] = api.render(result.middle_json, target, assets=result.assets, options=render_options)
            except Exception as error:
                artifacts[target.value] = {
                    "error_type": type(error).__name__,
                    "message": re.sub(r"0x[0-9A-Fa-f]+", "0xOBJECT", str(error)),
                }
            timings["render_" + target.value] = time.perf_counter() - render_start
        timings["multi_export"] = sum(value for key, value in timings.items() if key.startswith("render_"))
        timings["total"] = time.perf_counter() - started
    captured = {"source_sha256": source_hash, "pages": len(result.middle_json.pages)}
    if destination is not None:
        destination.mkdir(parents=True, exist_ok=True)
        if isinstance(source, bytes):
            (destination / "source.bin").write_bytes(source)
        outputs = {
            "model": result.model_json.model_dump(mode="json") if result.model_json else None,
            "middle": result.middle_json.model_dump(mode="json"),
            "assets": {key: digest(value) for key, value in result.assets.items()},
            "renders": {},
        }
        for target, artifact in artifacts.items():
            if isinstance(artifact, dict):
                outputs["renders"][target] = artifact
                continue
            artifact.write(destination / target / ("result." + target))
            outputs["renders"][target] = artifact_signature(target, artifact.content)
        result.save_bundle(destination / "bundle")
        write_json(destination / "output.json", outputs)
        captured["render_errors"] = {
            key: value for key, value in outputs["renders"].items() if isinstance(value, dict) and "error_type" in value
        }
        captured["full_output_sha256"] = digest(json.dumps(outputs, ensure_ascii=False, sort_keys=True).encode())
    return timings, captured


def worker(name: str, output: Path, runs: int, profile: bool, source_file: Path | None = None) -> None:
    """Cold runs are measured first, then warmed-up runs are timed; memory sampling and profiling are performed independently."""
    from loguru import logger

    logger.disable("docvortex")
    source, options, source_hash = source_case(name)
    if source_file is not None:
        source = source_file.read_bytes()
        source_hash = digest(source)
    _CASE_CACHE[name] = source, options, source_hash
    cold_started = time.perf_counter()
    cold, _ = pipeline_once(name)
    cold["wall_with_imports"] = time.perf_counter() - cold_started
    measured = []
    for _ in range(runs):
        gc.collect()
        timings, _ = pipeline_once(name)
        measured.append(timings)
    sampler = MemorySampler()
    sampler.thread.start()
    try:
        pipeline_once(name)
    finally:
        memory = sampler.finish()
    _, captured = pipeline_once(name, output)
    record = {
        "path": name,
        **captured,
        "cold_seconds": cold,
        "warm_seconds": measured,
        "memory": memory,
        "median_seconds": {key: statistics.median(run.get(key, 0.0) for run in measured) for key in measured[0]}
        if measured
        else {},
    }
    if profile:
        profiler = cProfile.Profile()
        profiler.runcall(pipeline_once, name)
        record["profile"] = [
            {
                "file": file.split("docvortex/")[-1],
                "line": line,
                "function": function,
                "calls": data[1],
                "self_seconds": data[2],
                "cumulative_seconds": data[3],
            }
            for (file, line, function), data in pstats.Stats(profiler).stats.items()
            if "docvortex/" in file
        ]
    write_json(output / "result.json", record)


def main() -> None:
    """Start an independent interpreter for each corpus, and strictly compare the output from the same source and the same set."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--worker")
    parser.add_argument("--source", type=Path)
    args = parser.parse_args()
    if args.runs < 0 or (args.output / "report.json").exists():
        parser.error("runs must be nonnegative and output must not contain a report")
    if args.worker:
        worker(args.worker, args.output, args.runs, args.profile, args.source)
        return
    paths = args.path or [
        "demo/pdfs/2407.00079v4_origi-10.pdf",
        "demo/pdfs/中文论文4.pdf",
        "demo/pdfs/caibao1.pdf",
        *(
            "fixture:" + suffix
            for suffix in (
                "doc",
                "docx",
                "ppt",
                "pptx",
                "xls",
                "xlsx",
                "rtf",
                "html",
                "csv",
                "epub",
                "ofd",
                "odt",
                "ods",
                "odp",
            )
        ),
        "synthetic:continuations",
    ]
    records = []
    previous_sources = {}
    if args.baseline:
        old_report = json.loads((args.baseline / "report.json").read_text(encoding="utf-8"))
        previous_sources = {item["path"]: args.baseline / item["artifact"] / "source.bin" for item in old_report["documents"]}
    for index, name in enumerate(dict.fromkeys(paths)):
        destination = args.output.resolve() / f"{index:02d}"
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            name,
            "--output",
            str(destination),
            "--runs",
            str(args.runs),
        ]
        if args.profile:
            command.append("--profile")
        if name in previous_sources and previous_sources[name].is_file():
            command.extend(["--source", str(previous_sources[name].resolve())])
        subprocess.run(command, cwd=ROOT, check=True)
        record = json.loads((destination / "result.json").read_text(encoding="utf-8"))
        record["artifact"] = str(destination.relative_to(args.output.resolve()))
        records.append(record)
        print(f"{index + 1}/{len(paths)} {name}: {record['median_seconds'].get('total', 0):.3f}s", flush=True)
    report = {
        "documents": records,
        "runs": args.runs,
        "python": sys.version,
        "platform": platform.platform(),
        "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dependencies": {name: version(name) for name in ("docvortex", "pypdfium2", "numpy", "pydantic")},
    }
    write_json(args.output / "report.json", report)
    if args.baseline:
        old = json.loads((args.baseline / "report.json").read_text(encoding="utf-8"))
        previous = {item["path"]: item for item in old["documents"]}
        if set(previous) != {item["path"] for item in records}:
            raise SystemExit("Baseline and candidate corpus differ")
        comparisons = []
        for current in records:
            prior = previous[current["path"]]
            if prior["source_sha256"] != current["source_sha256"]:
                raise SystemExit("Source changed: " + current["path"])
            comparisons.append(
                {
                    "path": current["path"],
                    "equal": current["full_output_sha256"] == prior["full_output_sha256"],
                    "time_ratios": {
                        key: value / prior["median_seconds"][key]
                        for key, value in current["median_seconds"].items()
                        if prior["median_seconds"].get(key)
                    },
                    "rss_ratio": current["memory"]["sampled_tree_peak_rss_bytes"]
                    / prior["memory"]["sampled_tree_peak_rss_bytes"],
                }
            )
        write_json(args.output / "comparison.json", comparisons)
        if any(not item["equal"] for item in comparisons):
            raise SystemExit("Output differs; inspect output.json and comparison.json")


if __name__ == "__main__":
    main()
