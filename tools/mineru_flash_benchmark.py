"""Dual warehouse real MinerU Flash Automatic classification entry benchmark, timing and process tree RSS uses independent processes."""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import gc
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

# The host integration tool is placed in tools; it only reuses the independent benchmark sampler and does not introduce MinerU dependency to the engine test.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "benchmarks"))


class OutsideFlashText(RuntimeError):
    """When automatically classifying OCR, the model initialization is stopped and the sample is explicitly excluded."""


class RejectLocalModels:
    """Overrides the protection boundary of the model factory without modifying the classification or Flash text path."""

    def get_model(self, *args, **kwargs):
        """Flash directly rejects automatic classification when entering the model branch, and never loads the real model."""
        raise OutsideFlashText("Automatic classification selected OCR; outside the Flash text chain")


def reject_vlm(*args, **kwargs):
    """If the Flash benchmark abnormally requests VLM, it will explicitly fail to prevent model downloading or network inference."""
    raise RuntimeError("Flash benchmark must not initialize a VLM predictor")


def utc_now():
    """Logs the UTC boundary for the scheduler to audit for overlap with other timings."""
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    """Save the complete protocol outside the entrance under test, prohibiting non-finite numbers from creeping into the summary."""
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def digest(value):
    """Generate deterministic JSON summaries with text, material, geometry, and field order semantics fully preserved."""
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    return hashlib.sha256(data).hexdigest()


def read_payload(path):
    """Read PDF or existing XOR corpus and return a summary of the encoded file and decoded payload."""
    source = Path(path).read_bytes()
    payload = source
    encoding = "pdf"
    if Path(path).suffix.lower() == ".xor":
        key = b"MinerU flash layout fixture"
        payload = bytes(value ^ key[index % len(key)] for index, value in enumerate(source))
        encoding = "xor"
    return payload, {
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "source_encoding": encoding,
    }


def repository_identity(root, package_relative, *, native=False):
    """Freeze the actual package source code and build statement of the two warehouses respectively, and do not mistake the root directory of MinerU for the layout of src."""
    root = Path(root).resolve()
    package = root / package_relative
    if not (package / "__init__.py").is_file():
        raise FileNotFoundError(f"Package source is missing: {package}")
    files = list(package.rglob("*.py"))
    if native:
        files += list((root / "rust").rglob("*.rs"))
        files += list((root / "rust").rglob("*.toml"))
    for name in ("pyproject.toml", "setup.py", "Cargo.toml", "Cargo.lock", "rust-toolchain.toml"):
        if (root / name).is_file():
            files.append(root / name)
    fingerprint = hashlib.sha256()
    for path in sorted(set(files)):
        fingerprint.update(path.relative_to(root).as_posix().encode())
        fingerprint.update(path.read_bytes())
    marker = root / "benchmark-revision.json"
    commit = None
    if marker.is_file():
        commit = json.loads(marker.read_text()).get("commit")
    elif (root / ".git").exists():
        commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    return {
        "root": str(root),
        "source_commit": commit,
        "source_code_sha256": fingerprint.hexdigest(),
        "package": str((package / "__init__.py").resolve()),
    }


def source_identities(config):
    """Generate independent summaries based on the true layout of each bin, applicable to current checkout and frozen baselines."""
    return {
        "docvortex": repository_identity(config["docvortex_source"], "src/docvortex", native=True),
        "mineru": repository_identity(config["mineru_source"], "mineru"),
    }


def runtime(config):
    """Import the actual entrance and verify the sources of the two warehouses; only block the model factory and do not replace the automatic classification or parsing logic."""
    started = time.perf_counter()
    import docvortex
    import mineru
    from docvortex._compute_backend import backend_info
    from docvortex.document.pdf import images
    from mineru.backend.analysis.pdf import pipeline

    import_seconds = time.perf_counter() - started
    for name, package in (("docvortex", docvortex), ("mineru", mineru)):
        if str(Path(package.__file__).resolve()) != config["sources"][name]["package"]:
            raise AssertionError(f"Imported {name} differs from requested source")
    if source_identities(config) != config["sources"]:
        raise AssertionError("Source tree changed after the input configuration was frozen")
    compute = backend_info()
    if compute["backend"] != config["compute_backend"]:
        raise AssertionError("Actual compute backend differs from requested backend")
    selector = getattr(images, "get_pdf_render_backend", None)
    actual_render = selector() if callable(selector) else "legacy"
    if actual_render != config["render_backend"]:
        raise RuntimeError("Selected source cannot provide the requested render backend")
    if config["render_backend"] == "session":
        from docvortex.document.pdf import PDFDocument

        if not callable(getattr(PDFDocument, "get_render_session", None)):
            raise RuntimeError("Session mode requires document-owned render sessions")
    if not hasattr(pipeline, "HybridLocalModelContextSingleton") or not hasattr(pipeline, "get_vlm_predictor"):
        raise RuntimeError("Unsupported MinerU model initialization boundary; refusing an unguarded benchmark")
    pipeline.HybridLocalModelContextSingleton = RejectLocalModels
    pipeline.get_vlm_predictor = reject_vlm
    return (
        pipeline.analyze_pdf,
        backend_info,
        {
            "import_seconds": import_seconds,
            "sources": config["sources"],
            "compute_at_import": compute,
            "render_backend": actual_render,
            "render_selector_available": callable(selector),
            "python": sys.version,
            "model_guard": "only model factories blocked; analyze_pdf still receives parse_mode=auto",
        },
    )


def stable_compute(backend_info, initial):
    """Verify that the identity of the actual loaded extension does not change during the sampling period of this process, only allowing the call count to grow."""
    actual = backend_info()
    for field in ("backend", "protocol", "extension_sha256"):
        if actual.get(field) != initial.get(field):
            raise AssertionError(f"Compute identity changed during measurement: {field}")
    return actual


def shutdown_renderers(config):
    """Releases the actual selected render pool before exiting worker, compatible with older baselines without the session module."""
    from docvortex.document.pdf.images import shutdown_pdf_render_executor

    try:
        shutdown_pdf_render_executor()
    finally:
        if config["render_backend"] == "session":
            from docvortex.document.pdf.render_session import shutdown_pdf_render_sessions

            shutdown_pdf_render_sessions()


def frozen_payload(config):
    """Each independent process only consumes the frozen copy and verifies that the original file and decoded content have not changed."""
    payload, identity = read_payload(config["frozen_input"])
    if identity != config["input_identity"]:
        raise AssertionError("Frozen input differs from the scheduled document")
    return payload


def semantic_output(result):
    """Serialize the complete AnalysisResult, excluding only the top-level elapsed, retaining all model_list and geometry."""
    if not is_dataclass(result):
        raise TypeError("Unsupported AnalysisResult contract")
    if result.parse_mode != "txt" or result.effort != "flash":
        raise AssertionError("Actual analysis result is outside the Flash text chain")
    output = asdict(result)
    if "model_list" not in output or "layout_geometry" not in output or output["layout_geometry"] is None:
        raise AssertionError("Complete model_list and layout_geometry are required")
    output.pop("elapsed")
    return output


def call_entry(analyze_pdf, payload):
    """Timing includes classification, patching, geometry, and cleaning. Do not use result.elapsed to replace the complete entry time."""
    started = time.perf_counter()
    result = analyze_pdf(payload, effort="flash", parse_mode="auto")
    return result, time.perf_counter() - started


def timing_worker(config, output):
    """The first call of the new process is also used as the only warm-up, and then five hot calls are tested. The summary and output writing to disk are outside the timing."""
    started_at = utc_now()
    analyze_pdf, backend_info, metadata = runtime(config)
    payload = frozen_payload(config)
    try:
        first_started = time.perf_counter()
        try:
            first_result, first_seconds = call_entry(analyze_pdf, payload)
        except OutsideFlashText as exc:
            if source_identities(config) != config["sources"]:
                raise AssertionError("Source tree changed during automatic classification")
            record = {
                **metadata,
                "status": "excluded_ocr",
                "eligible_flash_text": False,
                "resolved_parse_mode": "ocr",
                "reason": str(exc),
                "real_models_started": False,
                "first_call_classification_and_cleanup_seconds": time.perf_counter() - first_started,
                "input_identity": config["input_identity"],
                "started_at_utc": started_at,
                "finished_at_utc": utc_now(),
                "compute": stable_compute(backend_info, metadata["compute_at_import"]),
            }
            write_json(output / "timing.json", record)
            return
        full_output = semantic_output(first_result)
        signature = digest(full_output)
        write_json(output / "full-output.json", full_output)
        del first_result, full_output
        gc.collect()
        seconds = []
        fingerprints = []
        for _ in range(config["runs"]):
            result, elapsed = call_entry(analyze_pdf, payload)
            seconds.append(elapsed)
            actual = digest(semantic_output(result))
            fingerprints.append(actual)
            if actual != signature:
                raise AssertionError("Repeated Flash output is not deterministic")
            del result
            gc.collect()
        if source_identities(config) != config["sources"]:
            raise AssertionError("Source tree changed during timing")
        write_json(
            output / "timing.json",
            {
                **metadata,
                "status": "measured",
                "eligible_flash_text": True,
                "resolved_parse_mode": "txt",
                "first_call": {
                    "seconds": first_seconds,
                    "fresh_worker": True,
                    "used_as_only_warmup": True,
                    "includes_import": False,
                    "includes_process_startup": False,
                },
                "warmup_runs": 1,
                "hot_seconds": seconds,
                "median_seconds": statistics.median(seconds),
                "hot_runs": config["runs"],
                "full_output_sha256": signature,
                "hot_output_sha256": fingerprints,
                "input_identity": config["input_identity"],
                "compute": stable_compute(backend_info, metadata["compute_at_import"]),
                "started_at_utc": started_at,
                "finished_at_utc": utc_now(),
                "timing_scope": "complete analyze_pdf including auto classification, assets, geometry and cleanup",
            },
        )
    finally:
        shutdown_renderers(config)


def memory_worker(config, output):
    """The real inlet RSS is sampled only once after independent warm-up, and the complete output summary is constructed after the sampling is completed."""
    from pdf_memory import EntryMemorySampler

    timed = json.loads((output / "timing.json").read_text())
    if not timed["eligible_flash_text"]:
        raise RuntimeError("OCR samples are excluded from Flash text memory measurement")
    started_at = utc_now()
    analyze_pdf, backend_info, metadata = runtime(config)
    for field in ("backend", "protocol", "extension_sha256"):
        if metadata["compute_at_import"].get(field) != timed["compute"].get(field):
            raise AssertionError(f"Memory replay compute identity changed: {field}")
    payload = frozen_payload(config)
    try:
        warmed, _ = call_entry(analyze_pdf, payload)
        if warmed.parse_mode != "txt":
            raise AssertionError("Memory warmup selected a different analysis branch")
        del warmed
        gc.collect()
        sampler = EntryMemorySampler()
        sampler.thread.start()
        try:
            result = analyze_pdf(payload, effort="flash", parse_mode="auto")
        finally:
            memory = sampler.finish()
        actual = digest(semantic_output(result))
        if actual != timed["full_output_sha256"]:
            raise AssertionError("Memory replay differs from complete timed output")
        if source_identities(config) != config["sources"]:
            raise AssertionError("Source tree changed during memory replay")
        write_json(
            output / "memory.json",
            {
                **metadata,
                "status": "measured",
                "memory": memory,
                "full_output_sha256": actual,
                "equal": True,
                "compute": stable_compute(backend_info, metadata["compute_at_import"]),
                "method": "isolated-worker-one-warmup-one-entry",
                "tree_scope": "all descendants including render workers and resource tracking",
                "capture_scope": "analyze_pdf before semantic output serialization",
                "started_at_utc": started_at,
                "finished_at_utc": utc_now(),
            },
        )
    finally:
        shutdown_renderers(config)


def spawn_worker(args, kind):
    """Explicitly set the path and backend of the dual warehouse import, and wait for the previous process to exit before allowing the next sampling."""
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join((str((args.docvortex_source / "src").resolve()), str(args.mineru_source.resolve()))),
            "DOCVORTEX_COMPUTE_BACKEND": args.backend,
            "DOCVORTEX_PDF_RENDER_BACKEND": args.render_backend,
            "DOCVORTEX_PDF_RENDER_THREADS": str(args.processes),
            "MINERU_PDF_RENDER_THREADS": str(args.processes),
            "MINERU_PROCESSING_WINDOW_SIZE": str(args.window),
            "LOGURU_LEVEL": "WARNING",
            "ORT_DISABLE_TELEMETRY": "1",
        }
    )
    environment.pop("MINERU_PROFILE_STAGES", None)
    subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--worker", kind], env=environment, check=True
    )


def main():
    """Freezes single input and two source trees; allows separate scheduling timing and subsequent memory worker."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--docvortex-source", type=Path, required=True)
    parser.add_argument("--mineru-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="新建的基准目录；memory 模式复用已完成计时目录")
    parser.add_argument("--backend", choices=("python", "rust"), required=True)
    parser.add_argument("--render-backend", choices=("legacy", "session"), default="legacy")
    parser.add_argument("--mode", choices=("timing", "memory", "both"), default="both")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--window", type=int, default=64)
    parser.add_argument("--processes", type=int, default=3)
    parser.add_argument("--worker", choices=("timing", "memory"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if min(args.runs, args.window, args.processes) < 1:
        parser.error("runs, window and processes must be positive")
    config_path = args.output / "config.json"
    if args.worker:
        config = json.loads(config_path.read_text())
        (timing_worker if args.worker == "timing" else memory_worker)(config, args.output)
        return
    requested = {
        "path": str(args.path.resolve()),
        "docvortex_source": str(args.docvortex_source.resolve()),
        "mineru_source": str(args.mineru_source.resolve()),
        "compute_backend": args.backend,
        "render_backend": args.render_backend,
        "runs": args.runs,
        "window": args.window,
        "processes": args.processes,
    }
    if args.mode == "memory":
        config = json.loads(config_path.read_text())
        if any(config[key] != value for key, value in requested.items()):
            parser.error("memory replay arguments differ from the frozen timing configuration")
        if (args.output / "memory.json").exists():
            parser.error("memory output already exists")
    else:
        if args.output.exists():
            parser.error("timing output directory must not already exist")
        source = args.path.read_bytes()
        _, identity = read_payload(args.path)
        if hashlib.sha256(source).hexdigest() != identity["source_sha256"]:
            raise AssertionError("Input changed while freezing")
        args.output.mkdir(parents=True)
        frozen = args.output / ("input.pdf.xor" if identity["source_encoding"] == "xor" else "input.pdf")
        frozen.write_bytes(source)
        config = {**requested, "frozen_input": str(frozen.resolve()), "input_identity": identity, "created_at_utc": utc_now()}
        config["sources"] = source_identities(config)
        write_json(config_path, config)
        spawn_worker(args, "timing")
    timed = json.loads((args.output / "timing.json").read_text())
    if args.mode in {"memory", "both"} and timed["eligible_flash_text"]:
        spawn_worker(args, "memory")
    memory_path = args.output / "memory.json"
    write_json(
        args.output / "report.json",
        {
            "scope": "MinerU analyze_pdf(effort=flash, parse_mode=auto), Flash text samples only",
            "config": config,
            "timing": timed,
            "memory": json.loads(memory_path.read_text()) if memory_path.exists() else None,
        },
    )


if __name__ == "__main__":
    main()
