"""Model recording/playback of the actual shared PDF portal: rendering, text, tables and materials are all executed realistically."""

from __future__ import annotations

import argparse
import gc
from dataclasses import asdict
import gzip
import hashlib
import json
import os
from pathlib import Path
import pickle
import statistics
import sys
import time
from types import SimpleNamespace

from mineru_model_tape import ModelTape, ModelProxy, ContextProxy
from mineru_flash_benchmark import repository_identity, utc_now


def write_json(path, value):
    """After the timer expires, the complete protocol and evidence are saved, and the serialization is not mixed into PDF, which is time-consuming."""
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")


def main():
    """Explicitly bind the dual warehouse source code and the trusted native tape, no recording errors or input differences are allowed to be silently ignored."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--docvortex-source", type=Path, required=True)
    parser.add_argument("--mineru-source", type=Path, required=True)
    parser.add_argument("--tape", type=Path, required=True, help="仅加载自己在本机创建的 pickle 模型录制文件")
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--memory", action="store_true", help="独立进程预热一次后只采样一次入口 RSS，不用于计时")
    parser.add_argument("--verify-inputs", action="store_true")
    parser.add_argument("--effort", choices=("medium", "high", "xhigh"), default="medium")
    parser.add_argument("--parse-mode", choices=("auto", "txt", "ocr"), default="auto")
    parser.add_argument("--backend", choices=("python", "rust"), default="rust")
    parser.add_argument("--render-backend", choices=("legacy", "session"), required=True)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    if args.output.exists() or args.runs < 1 or (args.record and args.tape.exists()):
        parser.error("Require positive runs and unused output/recording paths")
    if args.memory and (args.record or args.verify_inputs):
        parser.error("Memory sampling must be separate from recording and input audits")
    started_at = utc_now()
    identities = {
        "docvortex": repository_identity(args.docvortex_source, "src/docvortex", native=True),
        "mineru": repository_identity(args.mineru_source, "mineru"),
    }
    os.environ.update(
        DOCVORTEX_COMPUTE_BACKEND=args.backend,
        DOCVORTEX_PDF_RENDER_BACKEND=args.render_backend,
        ORT_DISABLE_TELEMETRY="1",
        LOGURU_LEVEL="WARNING",
    )
    # Recording is fixed using the existing local ONNX model and does not allow automatic device selection to change the inference path.
    os.environ.setdefault("MINERU_MODEL_SMALL_BACKEND", "onnx")
    sys.path[:0] = [str(args.docvortex_source.resolve() / "src"), str(args.mineru_source.resolve())]
    started = time.perf_counter()
    import docvortex
    import mineru
    import onnxruntime

    onnxruntime.disable_telemetry_events()
    from docvortex._compute_backend import backend_info
    from docvortex.document.pdf.images import shutdown_pdf_render_executor

    shutdown_pdf_render_sessions = None
    if args.render_backend == "session":
        from docvortex.document.pdf.render_session import shutdown_pdf_render_sessions
    from mineru.backend.analysis.pdf import pipeline

    import_seconds = time.perf_counter() - started
    assert Path(docvortex.__file__).resolve() == (args.docvortex_source / "src/docvortex/__init__.py").resolve()
    assert Path(mineru.__file__).resolve() == (args.mineru_source / "mineru/__init__.py").resolve()
    assert backend_info()["backend"] == args.backend
    payload = args.path.read_bytes()
    input_sha = hashlib.sha256(payload).hexdigest()
    if args.path.suffix == ".xor":
        key = b"MinerU flash layout fixture"
        payload = bytes(value ^ key[index % len(key)] for index, value in enumerate(payload))
    metadata = {
        "input_sha256": input_sha,
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "effort": args.effort,
        "parse_mode": args.parse_mode,
    }
    data = None
    if not args.record:
        with gzip.open(args.tape, "rb") as source:
            data = pickle.load(source)
        assert data["metadata"] == metadata, "Recording input or analysis options differ"
    tape = ModelTape(record=args.record, data=data, verify_inputs=args.verify_inputs)
    if args.record:
        tape.data["metadata"] = metadata
        tape.data["recording_sources"] = identities
    original_factory = pipeline.HybridLocalModelContextSingleton
    original_predictor = pipeline.get_vlm_predictor
    contexts = []

    def get_context():
        """Lazy initialization of the real model, only the first recording call requires model weights."""
        if not contexts:
            real = original_factory().get_model() if args.record else None
            contexts.append(ContextProxy(tape, real))
        return contexts[0]

    def get_predictor(config):
        """VLM also only freezes model boundaries; playback does not connect to the server or load the language model."""
        real, backend = original_predictor(config) if args.record else (None, "replay")
        return ModelProxy(tape, "vlm", lambda: real), backend

    pipeline.HybridLocalModelContextSingleton = lambda: SimpleNamespace(get_model=get_context)
    pipeline.get_vlm_predictor = get_predictor
    args.output.mkdir(parents=True)
    expected = json.loads(args.compare.read_text()) if args.compare else None
    observations = []
    memory = None
    initial_compute = backend_info()
    try:
        for index in range(1 if args.record else 2 if args.memory else args.runs + 1):
            tape.reset()
            sampler = None
            if args.memory and index == 1:
                from pdf_memory import EntryMemorySampler

                gc.collect()
                sampler = EntryMemorySampler()
                sampler.thread.start()
            started = time.perf_counter()
            try:
                result = pipeline.analyze_pdf(payload, effort=args.effort, parse_mode=args.parse_mode)
                elapsed = time.perf_counter() - started
            finally:
                if sampler is not None:
                    memory = sampler.finish()
            tape.complete()
            if args.memory and index == 0:
                del result
                continue
            output = asdict(result)
            output.pop("elapsed")
            output = json.loads(json.dumps(output, ensure_ascii=False))
            digest = hashlib.sha256(json.dumps(output, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if expected is None:
                expected = output
            assert output == expected, "Complete shared PDF output differs"
            if not args.record:
                assert digest == tape.data["output_sha256"], "Shared PDF output differs from recording"
            else:
                tape.data["output_sha256"] = digest
            if index == 0 or args.memory:
                write_json(args.output / "full-output.json", output)
            observations.append(
                {
                    "seconds": elapsed,
                    "model_callback_seconds": tape.callback_seconds,
                    "recorded_model_seconds": tape.model_seconds,
                    "model_call_counts": dict(tape.counts),
                    "output_sha256": digest,
                }
            )
            del result, output
        if args.record:
            args.tape.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(args.tape, "wb", compresslevel=1) as destination:
                pickle.dump(tape.data, destination, protocol=5)
        assert initial_compute["extension_sha256"] == backend_info()["extension_sha256"]
        assert identities == {
            "docvortex": repository_identity(args.docvortex_source, "src/docvortex", native=True),
            "mineru": repository_identity(args.mineru_source, "mineru"),
        }, "Source changed during measurement"
        write_json(
            args.output / "report.json",
            {
                "scope": "Actual analyze_pdf shared chain; real classification, rasterization, text/table evidence, assets and page geometry; only model calls frozen",
                "record": args.record,
                "timing_eligible": not args.record and not args.verify_inputs and not args.memory,
                "memory": memory,
                "memory_method": "isolated-entry-after-one-warmup" if args.memory else None,
                "sources": identities,
                "recording_sources": tape.data.get("recording_sources"),
                "started_at_utc": started_at,
                "ended_at_utc": utc_now(),
                "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "model_tape_harness_sha256": hashlib.sha256(
                    Path(__file__).with_name("mineru_model_tape.py").read_bytes()
                ).hexdigest(),
                "metadata": metadata,
                "tape_sha256": hashlib.sha256(args.tape.read_bytes()).hexdigest(),
                "import_seconds": import_seconds,
                "first_call": None if args.memory else observations[0],
                "observations": observations if args.memory else observations[1:],
                "median_seconds": statistics.median(row["seconds"] for row in observations[1:])
                if len(observations) > 1
                else None,
                "callback_time_policy": "Included in wall time, reported separately; input hashing enabled only in non-timing audits",
                "compute": backend_info(),
                "docvortex_source": docvortex.__file__,
                "mineru_source": mineru.__file__,
                "render_backend": args.render_backend,
            },
        )
    finally:
        pipeline.HybridLocalModelContextSingleton = original_factory
        pipeline.get_vlm_predictor = original_predictor
        shutdown_pdf_render_executor()
        if shutdown_pdf_render_sessions is not None:
            shutdown_pdf_render_sessions()


if __name__ == "__main__":
    main()
