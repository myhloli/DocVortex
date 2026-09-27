"""双仓真实 MinerU Flash 自动分类入口基准，计时与进程树 RSS 使用独立进程。"""

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

# 宿主集成工具放在 tools；只复用独立基准采样器，不给引擎测试引入 MinerU 依赖。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "benchmarks"))


class OutsideFlashText(RuntimeError):
    """自动分类走 OCR 时中止模型初始化，显式排除该样本。"""


class RejectLocalModels:
    """替代模型工厂的保护边界，不修改分类或 Flash 文本路径。"""

    def get_model(self, *args, **kwargs):
        """Flash 自动分类进入模型分支时直接拒绝，绝不加载真实模型。"""
        raise OutsideFlashText("Automatic classification selected OCR; outside the Flash text chain")


def reject_vlm(*args, **kwargs):
    """Flash 基准若异常请求 VLM，明确失败，防止模型下载或网络推理。"""
    raise RuntimeError("Flash benchmark must not initialize a VLM predictor")


def utc_now():
    """记录 UTC 边界，供调度器审计与其他计时的重叠。"""
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    """在被测入口之外保存完整协议，禁止非有限数字悄悄进入摘要。"""
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def digest(value):
    """完整保留正文、素材、几何和字段顺序语义，生成确定性 JSON 摘要。"""
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    return hashlib.sha256(data).hexdigest()


def read_payload(path):
    """读取 PDF 或既有 XOR 语料，同时返回编码文件与解码载荷的摘要。"""
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
    """分别冻结两仓实际包源码与构建声明，不把 MinerU 的根目录误认成 src 布局。"""
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
    """以各仓真实布局生成独立摘要，适用于当前 checkout 和冻结基线。"""
    return {
        "docvortex": repository_identity(config["docvortex_source"], "src/docvortex", native=True),
        "mineru": repository_identity(config["mineru_source"], "mineru"),
    }


def runtime(config):
    """导入实际入口并验证两仓来源；只封锁模型工厂，不替换自动分类或解析逻辑。"""
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
    """确认实际加载扩展的身份在本进程采样期间未改变，只允许调用计数增长。"""
    actual = backend_info()
    for field in ("backend", "protocol", "extension_sha256"):
        if actual.get(field) != initial.get(field):
            raise AssertionError(f"Compute identity changed during measurement: {field}")
    return actual


def shutdown_renderers(config):
    """退出 worker 前释放实际选择的渲染池，兼容没有 session 模块的旧基线。"""
    from docvortex.document.pdf.images import shutdown_pdf_render_executor

    try:
        shutdown_pdf_render_executor()
    finally:
        if config["render_backend"] == "session":
            from docvortex.document.pdf.render_session import shutdown_pdf_render_sessions

            shutdown_pdf_render_sessions()


def frozen_payload(config):
    """每个独立进程只消费冻结副本，并验证原始文件及解码内容未变化。"""
    payload, identity = read_payload(config["frozen_input"])
    if identity != config["input_identity"]:
        raise AssertionError("Frozen input differs from the scheduled document")
    return payload


def semantic_output(result):
    """序列化完整 AnalysisResult，仅排除顶层 elapsed，保留全部 model_list 和 geometry。"""
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
    """计时包含分类、补图、几何及清理，不使用 result.elapsed 代替完整入口耗时。"""
    started = time.perf_counter()
    result = analyze_pdf(payload, effort="flash", parse_mode="auto")
    return result, time.perf_counter() - started


def timing_worker(config, output):
    """新进程首调用同时作为唯一预热，再测五次热调用，摘要与输出写盘均在计时外。"""
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
    """独立预热后仅采样一次真实入口 RSS，结束采样后才构造完整输出摘要。"""
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
    """显式设置双仓 import 路径与后端，等待前一进程退出后才允许下一次采样。"""
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
    """冻结单份输入和两个源码树；允许分开调度计时与后续内存 worker。"""
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
