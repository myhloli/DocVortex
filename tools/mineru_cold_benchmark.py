"""Cold running benchmark from starting the interpreter to the first actual shared parse return; model tape loads a single column but does not deduct it from the total elapsed time."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time


def child(args):
    """The new process is analyzed only once, and the return time is recorded before output summary and source verification."""
    entered = time.monotonic_ns()
    os.environ.update(
        DOCVORTEX_COMPUTE_BACKEND="rust",
        DOCVORTEX_PDF_RENDER_BACKEND=args.render_backend,
        ORT_DISABLE_TELEMETRY="1",
        LOGURU_LEVEL="WARNING",
    )
    sys.path[:0] = [str(args.docvortex_source.resolve() / "src"), str(args.mineru_source.resolve())]
    from dataclasses import asdict
    import gzip
    import pickle
    from types import SimpleNamespace
    from mineru_model_tape import ModelTape, ModelProxy, ContextProxy
    import docvortex
    import mineru
    import onnxruntime
    from docvortex._compute_backend import backend_info
    from docvortex.document.pdf.images import shutdown_pdf_render_executor
    from mineru.backend.analysis.pdf import pipeline

    onnxruntime.disable_telemetry_events()
    imported = time.monotonic_ns()
    payload = args.path.read_bytes()
    if args.path.suffix == ".xor":
        key = b"MinerU flash layout fixture"
        payload = bytes(value ^ key[index % len(key)] for index, value in enumerate(payload))
    with gzip.open(args.tape, "rb") as stream:
        data = pickle.load(stream)
    assert data["metadata"]["payload_sha256"] == hashlib.sha256(payload).hexdigest()
    assert data["metadata"]["effort"] == args.effort and data["metadata"]["parse_mode"] == "auto"
    tape = ModelTape(record=False, data=data, verify_inputs=False)
    context = ContextProxy(tape, None)
    original_factory, original_predictor = pipeline.HybridLocalModelContextSingleton, pipeline.get_vlm_predictor
    pipeline.HybridLocalModelContextSingleton = lambda: SimpleNamespace(get_model=lambda: context)
    pipeline.get_vlm_predictor = lambda config: (ModelProxy(tape, "vlm", lambda: None), "replay")
    prepared = time.monotonic_ns()
    try:
        tape.reset()
        result = pipeline.analyze_pdf(payload, effort=args.effort, parse_mode="auto")
        ready = time.monotonic_ns()
        tape.complete()
        output = asdict(result)
        output.pop("elapsed")
        output = json.loads(json.dumps(output, ensure_ascii=False))
        digest = hashlib.sha256(json.dumps(output, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        assert digest == data["output_sha256"], "Cold output differs from real model recording"
        assert Path(docvortex.__file__).resolve() == (args.docvortex_source / "src/docvortex/__init__.py").resolve()
        assert Path(mineru.__file__).resolve() == (args.mineru_source / "mineru/__init__.py").resolve()
        report = {
            "child_enter_ns": entered,
            "imported_ns": imported,
            "prepared_ns": prepared,
            "result_ready_ns": ready,
            "output_sha256": digest,
            "compute": backend_info(),
            "model_callback_seconds": tape.callback_seconds,
            "model_call_counts": dict(tape.counts),
        }
    finally:
        pipeline.HybridLocalModelContextSingleton, pipeline.get_vlm_predictor = original_factory, original_predictor
        shutdown_pdf_render_executor()
        if args.render_backend == "session":
            from docvortex.document.pdf.render_session import shutdown_pdf_render_sessions

            shutdown_pdf_render_sessions()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))


def main():
    """The parent process first verifies the source code and then starts five independent interpreters. Neither shutdown nor result serialization is mixed into the first ready time."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("path", "docvortex-source", "mineru-source", "tape", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--effort", choices=("medium", "high", "xhigh"), default="medium")
    parser.add_argument("--render-backend", choices=("legacy", "session"), required=True)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--child", action="store_true")
    args = parser.parse_args()
    if args.child:
        child(args)
        return
    if args.output.exists() or args.runs < 1:
        parser.error("Require unused output and positive runs")
    from mineru_flash_benchmark import repository_identity

    def identities():
        """Check the source code of the two warehouses outside the timing zone to avoid source code hash overhead disguised as parsing cold start."""
        return {
            "docvortex": repository_identity(args.docvortex_source, "src/docvortex", native=True),
            "mineru": repository_identity(args.mineru_source, "mineru"),
        }

    source = identities()
    helper_paths = [Path(__file__), Path(__file__).with_name("mineru_model_tape.py")]
    helpers = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in helper_paths}
    args.output.mkdir(parents=True)
    tape_sha = hashlib.sha256(args.tape.read_bytes()).hexdigest()
    observations = []
    for index in range(args.runs):
        destination = args.output / f"run-{index}.json"
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--child",
            "--output",
            str(destination),
            "--path",
            str(args.path),
            "--tape",
            str(args.tape),
            "--effort",
            args.effort,
            "--docvortex-source",
            str(args.docvortex_source),
            "--mineru-source",
            str(args.mineru_source),
            "--render-backend",
            args.render_backend,
        ]
        with destination.with_suffix(".log").open("w") as log:
            started = time.monotonic_ns()
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        record = json.loads(destination.read_text())
        record.update(
            seconds=(record["result_ready_ns"] - started) / 1e9,
            interpreter_and_harness_seconds=(record["child_enter_ns"] - started) / 1e9,
            imports_seconds=(record["imported_ns"] - record["child_enter_ns"]) / 1e9,
            input_and_tape_seconds=(record["prepared_ns"] - record["imported_ns"]) / 1e9,
            first_parse_seconds=(record["result_ready_ns"] - record["prepared_ns"]) / 1e9,
        )
        observations.append(record)
    assert identities() == source and hashlib.sha256(args.tape.read_bytes()).hexdigest() == tape_sha
    assert helpers == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in helper_paths}
    assert len({item["output_sha256"] for item in observations}) == 1
    assert len({item["compute"]["extension_sha256"] for item in observations}) == 1
    report = {
        "scope": "New interpreter through first analyze_pdf return; includes harness bootstrap, imports, input/tape loading, font and worker startup; excludes actual model inference, post-return validation and shutdown",
        "sources": source,
        "tape_sha256": tape_sha,
        "harness_sha256": helpers[Path(__file__).name],
        "model_tape_harness_sha256": helpers["mineru_model_tape.py"],
        "observations": observations,
        "median_seconds": statistics.median(item["seconds"] for item in observations),
        "warmup": "No in-process warmup; ordinary OS filesystem caches are not purged",
    }
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
