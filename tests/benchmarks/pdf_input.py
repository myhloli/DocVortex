"""输入 PDF 的分类、字符/矢量提取和栅格化分账，不混入导出 PDF 或模型推理。"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time


def read_input(path):
    """读取语料并按既有固定密钥解码 XOR，分别保留文件与解析载荷摘要。"""
    source = path.read_bytes()
    payload = source
    encoding = "pdf"
    if path.suffix.lower() == ".xor":
        key = b"MinerU flash layout fixture"
        payload = bytes(value ^ key[index % len(key)] for index, value in enumerate(source))
        encoding = "xor"
    return payload, hashlib.sha256(source).hexdigest(), encoding


def resolve_render_mode(args, images, document_class):
    """验证实际渲染选择；旧基线仅允许 legacy，缺少 session 能力直接失败。"""
    selector = getattr(images, "get_pdf_render_backend", None)
    factory = getattr(document_class, "get_render_session", None)
    if selector is None:
        if args.render_backend != "legacy":
            raise RuntimeError("Requested session rendering is unavailable in the selected baseline")
        actual = "legacy"
    else:
        actual = selector()
    if actual != args.render_backend:
        raise AssertionError("actual render backend differs from requested backend")
    if actual == "session" and args.render_session_scope == "document" and not callable(factory):
        raise RuntimeError("Selected source does not support document-owned render sessions")
    return {
        "requested_backend": args.render_backend,
        "actual_backend": actual,
        "requested_session_scope": args.render_session_scope,
        "actual_session_scope": args.render_session_scope if actual == "session" else "none",
        "selector_available": callable(selector),
        "document_session_available": callable(factory),
        "input_binding": "document" if actual == "session" and args.render_session_scope == "document" else "bytes",
    }


def worker(args):
    """独立进程预热后计时，像素摘要与结果检查放在各阶段计时之外。"""
    started = time.perf_counter()
    from docvortex.document.pdf import PDFDocument, images as image_service
    from docvortex.document.pdf.images import load_images_from_pdf_bytes_range, shutdown_pdf_render_executor
    from docvortex._compute_backend import backend_info
    import docvortex

    import_seconds = time.perf_counter() - started
    if Path(docvortex.__file__).resolve() != (args.source / "src/docvortex/__init__.py").resolve():
        raise AssertionError("imported package differs from requested source")
    if backend_info()["backend"] != args.backend:
        raise AssertionError("actual compute backend differs from requested backend")
    render_mode = resolve_render_mode(args, image_service, PDFDocument)
    shutdown_sessions = None
    if args.render_backend == "session":
        from docvortex.document.pdf.render_session import shutdown_pdf_render_sessions

        shutdown_sessions = shutdown_pdf_render_sessions
    payload, source_sha256, source_encoding = read_input(args.path)
    samples = defaultdict(list)
    signatures = []
    session_diagnostics = []
    try:
        for run in range(args.runs + 1):
            durations = defaultdict(float)
            fingerprint = hashlib.sha256()
            started = time.perf_counter()
            document = PDFDocument(payload)
            render_session = None
            try:
                pages = len(document)
                durations["open"] = time.perf_counter() - started
                started = time.perf_counter()
                kind = document.classify()
                durations["classify"] = time.perf_counter() - started
                fingerprint.update(kind.encode())
                for index in range(pages):
                    page = document[index]
                    started = time.perf_counter()
                    geometry = page.get_chars_with_geometry()
                    durations["text"] += time.perf_counter() - started
                    started = time.perf_counter()
                    vectors = page.get_vector_geometry()
                    durations["vectors"] += time.perf_counter() - started
                    # 该摘要只用于重复运行稳定性；完整结果差分仍由 rust_pdf 基准负责。
                    state = [{k: list(v) if k == "bbox" else v for k, v in c.items()} for c in geometry.chars]
                    fingerprint.update(json.dumps(state, sort_keys=True).encode())
                    fingerprint.update(repr(vectors).encode())
                for first in range(0, pages, args.window):
                    started = time.perf_counter()
                    if render_mode["input_binding"] == "document" and render_session is None:
                        render_session = document.get_render_session(threads=args.processes)
                    images = load_images_from_pdf_bytes_range(
                        payload,
                        start_page_id=first,
                        end_page_id=min(first + args.window, pages) - 1,
                        threads=args.processes,
                        **({"session": render_session} if render_session is not None else {}),
                    )
                    durations["raster_pool"] += time.perf_counter() - started
                    try:
                        for item in images:
                            image = item["img_pil"]
                            fingerprint.update(repr((image.mode, image.size, item["scale"])).encode())
                            fingerprint.update(image.tobytes())
                    finally:
                        for item in images:
                            item["img_pil"].close()
            finally:
                started = time.perf_counter()
                document.close()
                durations["close"] += time.perf_counter() - started
                if render_session is not None:
                    session_diagnostics.append(
                        {
                            "run": run,
                            "document_opens": len(render_session.worker_diagnostics),
                            "worker_pids": [item["pid"] for item in render_session.worker_diagnostics],
                            "input_released": len(render_session.close_diagnostics) == len(render_session.worker_diagnostics)
                            and all(item["input_released"] for item in render_session.close_diagnostics),
                        }
                    )
            signatures.append(fingerprint.hexdigest())
            if run == 0:
                first_seconds = dict(durations)
            else:
                for stage, seconds in durations.items():
                    samples[stage].append(seconds)
        if len(set(signatures)) != 1:
            raise AssertionError("input service produced non-deterministic output")
        from rust_pdf import source_identity

        result = {
            "scope": "PDF input services; excludes model inference and PDF export",
            "source_sha256": source_sha256,
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "source_encoding": source_encoding,
            "render": render_mode,
            "render_sessions": session_diagnostics,
            "pages": pages,
            "source_package": docvortex.__file__,
            **source_identity(Path(docvortex.__file__)),
            "compute": backend_info(),
            "python": sys.version,
            "window": args.window,
            "processes": args.processes,
            "import_seconds": import_seconds,
            "first_seconds": first_seconds,
            "seconds": dict(samples),
            "median_seconds": {k: statistics.median(v) for k, v in samples.items()},
            "output_sha256": signatures[0],
        }
        args.output.write_text(json.dumps(result, indent=2))
    finally:
        try:
            shutdown_pdf_render_executor()
        finally:
            if shutdown_sessions is not None:
                shutdown_sessions()


def main():
    """显式冻结实际导入的源码及后端，禁止覆盖既有记录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--backend", choices=("python", "rust"), required=True)
    parser.add_argument("--render-backend", choices=("legacy", "session"), default="legacy")
    parser.add_argument(
        "--render-session-scope",
        choices=("document", "bytes"),
        default="document",
        help="session 模式按文档复用或每个 bytes 窗口创建短会话；legacy 忽略该项",
    )
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--window", type=int, default=4)
    parser.add_argument("--processes", type=int, default=3)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if min(args.runs, args.window, args.processes) < 1 or args.output.exists():
        parser.error("positive limits and an unused output path are required")
    if args.worker:
        os.environ["DOCVORTEX_PDF_RENDER_BACKEND"] = args.render_backend
        worker(args)
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    environment = dict(
        os.environ,
        PYTHONPATH=str((args.source / "src").resolve()),
        DOCVORTEX_COMPUTE_BACKEND=args.backend,
        DOCVORTEX_PDF_RENDER_BACKEND=args.render_backend,
        LOGURU_LEVEL="WARNING",
    )
    started = time.perf_counter()
    subprocess.run([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--worker"], env=environment, check=True)
    result = json.loads(args.output.read_text())
    result["process_wall_seconds"] = time.perf_counter() - started
    result["process_wall_scope"] = "startup, all runs, validation, serialization and shutdown; not a cold parse metric"
    args.output.write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
