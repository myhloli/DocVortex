"""The independent ledger is fully disclosed parse; by default only the complete stage is counted, and the detailed cProfile must be run as another diagnosis."""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict
import hashlib
import cProfile
import gc
from functools import wraps
import json
import os
from pathlib import Path
import sys
import time


def main():
    """Only wraps high-level orchestration boundaries, preserving character extractor/rule function identity and native path selection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--render-backend", choices=("legacy", "session"), required=True)
    parser.add_argument("--cpu-profile", action="store_true")
    args = parser.parse_args()
    if args.output.exists() or args.runs < 1:
        parser.error("Require unused output and positive runs")
    os.environ.update(
        DOCVORTEX_COMPUTE_BACKEND="rust",
        DOCVORTEX_PDF_RENDER_BACKEND=args.render_backend,
        ORT_DISABLE_TELEMETRY="1",
        LOGURU_LEVEL="WARNING",
    )
    sys.path[:0] = [str(args.source.resolve() / "src"), str(Path(__file__).resolve().parents[1] / "tests/benchmarks")]
    import onnxruntime

    onnxruntime.disable_telemetry_events()
    import docvortex
    from docvortex._compute_backend import backend_info
    from docvortex.analyzers.native.pdf import pipeline
    from docvortex.document.pdf.images import shutdown_pdf_render_executor
    from rust_pdf import public_once, digest, source_identity

    assert Path(docvortex.__file__).resolve() == (args.source / "src/docvortex/__init__.py").resolve()
    identity = source_identity(Path(docvortex.__file__))
    payload = args.path.read_bytes()
    if args.path.suffix == ".xor":
        key = b"MinerU flash layout fixture"
        payload = bytes(value ^ key[index % len(key)] for index, value in enumerate(payload))
    _, warm = public_once(payload)
    expected = digest(warm)
    del warm
    stages = defaultdict(lambda: {"calls": 0, "inclusive_seconds": 0.0, "exclusive_seconds": 0.0})
    stack = []
    originals = {}

    def wrap(name, function):
        """Only entry and exit are recorded, the nested stage deducts the time spent in sub-stages, and does not touch characters or rule parameters."""

        @wraps(function)
        def measured(*values, **keywords):
            """Ensure that exceptions can also complete timing stack cleaning, and the return value of the original function and the exception remain unchanged."""
            frame = [time.perf_counter(), 0.0]
            stack.append(frame)
            try:
                return function(*values, **keywords)
            finally:
                elapsed = time.perf_counter() - frame[0]
                assert stack.pop() is frame
                row = stages[name]
                row["calls"] += 1
                row["inclusive_seconds"] += elapsed
                row["exclusive_seconds"] += elapsed - frame[1]
                if stack:
                    stack[-1][1] += elapsed

        return measured

    for name in [
        "_collect_document_sources",
        "_prepare_document_sources",
        "build_document_geometry_plan",
        "_prepare_page_source",
        "detect_pdf_text_style_lines",
        "detect_pdf_text_link_lines",
        "detect_pdf_text_script_lines",
        "_classify_document_text",
        "_finalize_prepared_page",
        "_materialize_document_inline",
    ]:
        originals[name] = getattr(pipeline, name)
        setattr(pipeline, name, wrap(name, originals[name]))
    profile = cProfile.Profile() if args.cpu_profile else None
    elapsed = []
    before = backend_info()
    try:
        for _ in range(args.runs):
            gc.collect()
            if profile is not None:
                profile.enable()
            started = time.perf_counter()
            result = docvortex.parse(payload, file_suffix="pdf", keep_model_json=True)
            elapsed.append(time.perf_counter() - started)
            if profile is not None:
                profile.disable()
            output = {
                "model": result.model_json.model_dump(mode="json"),
                "middle": result.middle_json.model_dump(mode="json"),
                "assets": {key: hashlib.sha256(value).hexdigest() for key, value in result.assets.items()},
                "diagnostics": [asdict(item) for item in result.diagnostics],
            }
            assert digest(output) == expected, "Instrumentation changed complete public output"
            del result, output
    finally:
        if profile is not None:
            profile.disable()
        for name, function in originals.items():
            setattr(pipeline, name, function)
        shutdown_pdf_render_executor()
        if args.render_backend == "session":
            from docvortex.document.pdf.render_session import shutdown_pdf_render_sessions

            shutdown_pdf_render_sessions()
    after = backend_info()
    assert identity == source_identity(Path(docvortex.__file__)) and before["extension_sha256"] == after["extension_sha256"]
    args.output.mkdir(parents=True)
    if profile is not None:
        profile.dump_stats(str(args.output / "cpu.prof"))
    report = {
        "timing_eligible": False,
        "gc_policy": "Full collection before each measured parse, outside profiler; matches frozen public benchmark",
        "scope": "Separate stage diagnosis; no formal speed claim",
        "cprofile_enabled": bool(profile),
        "source": identity,
        "source_package": docvortex.__file__,
        "complete_output_sha256": expected,
        "observations_seconds": elapsed,
        "stages": dict(stages),
        "compute_before": before,
        "compute_after": after,
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
