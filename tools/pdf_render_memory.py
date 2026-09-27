"""隔离比较 PDF 输入栅格化的进程树 RSS 与连续文档资源释放，不执行像素摘要。"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gc
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys

BENCHMARKS = Path(__file__).resolve().parents[1] / "tests" / "benchmarks"


def benchmark_helpers():
    """只加载 DocVortex 的既有采样与身份工具，不导入 MinerU 或模型运行时。"""
    location = str(BENCHMARKS)
    if location not in sys.path:
        sys.path.insert(0, location)
    from pdf_input import read_input
    from pdf_memory import EntryMemorySampler
    from rust_pdf import source_identity

    return read_input, EntryMemorySampler, source_identity


def write_json(path, value):
    """仅在采样停止后序列化小型审计记录，不生成像素字节或完整输出 JSON。"""
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def utc_now():
    """记录执行边界，便于避免和正式时间基准发生竞争。"""
    return datetime.now(timezone.utc).isoformat()


def process_tree():
    """读取当前时刻完整进程树 RSS，包含渲染与资源跟踪进程，不相加独立峰值。"""
    import psutil

    parent = psutil.Process()
    values = {}
    helpers = []
    for process in (parent, *parent.children(recursive=True)):
        try:
            values[str(process.pid)] = process.memory_info().rss
            if process.pid != parent.pid:
                command = " ".join(process.cmdline())
                for role in ("resource_tracker", "forkserver"):
                    if f"multiprocessing.{role}" in command:
                        helpers.append({"pid": process.pid, "create_time": process.create_time(), "role": role})
                        break
        except psutil.NoSuchProcess:
            continue
    return {"rss_bytes": sum(values.values()), "process_count": len(values), "rss_by_pid": values, "helper_processes": helpers}


def worker_identities(pids):
    """在 worker 可观察时冻结 PID 与 OS 创建时间，拒绝误认 PID 重用后的其他进程。"""
    import psutil

    identities = []
    for pid in pids:
        process = psutil.Process(pid)
        if process.ppid() != os.getpid():
            raise RuntimeError(f"Registered render worker is no longer a child of this process: {pid}")
        identities.append({"pid": pid, "create_time": process.create_time()})
    return identities


def verify_os_worker_exit(identities):
    """关闭池后查询实际 OS 身份，僵尸或仍存活的同身份进程都记为残留。"""
    import psutil

    exited = []
    reused = []
    remaining = []
    errors = []
    for identity in identities:
        try:
            process = psutil.Process(identity["pid"])
            created = process.create_time()
            if created != identity["create_time"]:
                reused.append({**identity, "current_create_time": created})
            elif process.is_running():
                remaining.append({**identity, "status": process.status()})
            else:
                exited.append(identity)
        except psutil.NoSuchProcess:
            exited.append(identity)
        except psutil.Error as error:
            errors.append({**identity, "error": str(error)})
    return {
        "observed_workers": identities,
        "exited_workers": exited,
        "reused_pids": reused,
        "remaining_workers": remaining,
        "inspection_errors": errors,
        "all_observed_workers_exited": not remaining and not errors,
        "scope": "registered render worker PID plus create_time; excludes tracker/forkserver helpers",
    }


def pool_state(render_backend):
    """读取本进程拥有的池状态；只记录元数据，不触碰 PDFium 句柄或其他任务。"""
    from docvortex.document.pdf import images

    if render_backend == "session":
        from docvortex.document.pdf import render_session

        with render_session._worker_budget_lock:
            workers = list(render_session._all_workers)
            pids = sorted(process.pid for process, _ in workers if process.is_alive())
            return {
                "worker_pids": pids,
                "worker_identities": worker_identities(pids),
                "worker_count": len(pids),
                "idle_workers": len(render_session._idle_workers),
                "active_document_leases": len(render_session._leased_workers),
            }
    with images._pdf_render_executor_lock:
        executor = images._pdf_render_executor
        processes = {} if executor is None else (getattr(executor, "_processes", None) or {})
        pids = sorted(process.pid for process in processes.values() if process.is_alive())
        return {
            "worker_pids": pids,
            "worker_identities": worker_identities(pids),
            "worker_count": len(pids),
            "pending_tasks": 0 if executor is None else len(getattr(executor, "_pending_work_items", {})),
        }


def render_once(payload, config):
    """新建一份文档并渲染全部窗口，每窗口立即关闭页图，结束时释放文档租约。"""
    from docvortex.document.pdf import PDFDocument
    from docvortex.document.pdf.images import load_images_from_pdf_bytes_range

    document = PDFDocument(payload)
    session = None
    images = []
    sizes = []
    image_count = 0
    input_path = directory = None
    try:
        page_count = len(document)
        if config["render_backend"] == "session":
            session = document.get_render_session(threads=config["processes"], timeout=config["timeout"])
            input_path = session._input
            directory = input_path.parent
        for first in range(0, page_count, config["window"]):
            end = min(page_count, first + config["window"]) - 1
            images = load_images_from_pdf_bytes_range(
                payload,
                start_page_id=first,
                end_page_id=end,
                threads=config["processes"],
                timeout=config["timeout"],
                **({"session": session} if session is not None else {}),
            )
            try:
                if len(images) != end - first + 1:
                    raise AssertionError("Rendered image count differs from requested window")
                for offset, item in enumerate(images):
                    image = item["img_pil"]
                    # 只读取现有图像元数据；禁止 tobytes、数组化或编码制造额外大分配。
                    sizes.append(
                        {
                            "page": first + offset,
                            "width": image.width,
                            "height": image.height,
                            "mode": image.mode,
                            "scale": item["scale"],
                        }
                    )
                    image_count += 1
            finally:
                for item in images:
                    item["img_pil"].close()
                images.clear()
    finally:
        try:
            for item in images:
                item["img_pil"].close()
        finally:
            document.close()
    ownership = {"applicable": session is not None}
    if session is not None:
        ownership.update(
            {
                "session_closed": session._closed,
                "input_file_removed": not input_path.exists(),
                "temporary_directory_removed": not directory.exists(),
                "document_opens": len(session.worker_diagnostics),
                "close_acknowledgements": len(session.close_diagnostics),
                "input_maps_released": len(session.close_diagnostics) == len(session.worker_diagnostics)
                and all(item["input_released"] for item in session.close_diagnostics),
                "leased_workers_released": not session._workers,
            }
        )
    else:
        ownership["scope"] = "legacy byte copies and PDFium internals are not directly observable; pool tasks are checked"
    return {"page_count": page_count, "image_count": image_count, "images": sizes, "ownership": ownership}


def verify_resources(result, state):
    """关闭确认和文件检查失败时明确拒收，避免把低 RSS 误报为正确释放。"""
    if result["image_count"] != result["page_count"]:
        raise AssertionError("Not every document page was rendered")
    ownership = result["ownership"]
    if ownership["applicable"]:
        for field in (
            "session_closed",
            "input_file_removed",
            "temporary_directory_removed",
            "input_maps_released",
            "leased_workers_released",
        ):
            if not ownership[field]:
                raise AssertionError(f"Render session resource was not released: {field}")
        if state["active_document_leases"] != 0:
            raise AssertionError("A document lease remains active after rendering")
    elif state["pending_tasks"]:
        raise AssertionError("Legacy rendering tasks remain pending after rendering")


def shutdown(render_backend):
    """退出独立 worker 前关闭其唯一渲染池，不操作其他进程树的资源。"""
    from docvortex.document.pdf.images import shutdown_pdf_render_executor

    try:
        shutdown_pdf_render_executor()
    finally:
        if render_backend == "session":
            from docvortex.document.pdf.render_session import shutdown_pdf_render_sessions

            shutdown_pdf_render_sessions()


def memory_worker(config, output):
    """唯一预热后逐次采样独立文档入口，JSON、源码摘要和 GC 均置于采样区间之外。"""
    read_input, sampler_class, source_identity = benchmark_helpers()
    import docvortex
    from docvortex._compute_backend import backend_info
    from docvortex.document.pdf import PDFDocument, images

    package = Path(docvortex.__file__).resolve()
    if package != (Path(config["source"]) / "src/docvortex/__init__.py").resolve():
        raise AssertionError("Imported DocVortex differs from requested source")
    if source_identity(package) != config["source_identity"]:
        raise AssertionError("Source identity differs from the frozen configuration")
    selector = getattr(images, "get_pdf_render_backend", None)
    actual_render = selector() if callable(selector) else "legacy"
    if actual_render != config["render_backend"]:
        raise RuntimeError("Requested render backend is unavailable in selected source")
    if actual_render == "session" and not callable(getattr(PDFDocument, "get_render_session", None)):
        raise RuntimeError("Selected source lacks document-owned render sessions")
    compute = backend_info()
    if compute["backend"] != config["compute_backend"]:
        raise AssertionError("Actual compute backend differs from requested backend")
    payload, source_hash, encoding = read_input(Path(config["frozen_input"]))
    if source_hash != config["source_sha256"] or encoding != config["source_encoding"]:
        raise AssertionError("Frozen input file changed")
    if hashlib.sha256(payload).hexdigest() != config["payload_sha256"]:
        raise AssertionError("Decoded frozen PDF changed")
    started_at = utc_now()
    try:
        warmed = render_once(payload, config)
        warmed_pool = pool_state(actual_render)
        verify_resources(warmed, warmed_pool)
        shape = warmed["images"]
        del warmed
        gc.collect()
        warmed_tree = process_tree()
        observations = []
        for iteration in range(config["iterations"]):
            before = process_tree()
            sampler = sampler_class()
            sampler.thread.start()
            try:
                rendered = render_once(payload, config)
            finally:
                memory = sampler.finish()
            if rendered["images"] != shape:
                raise AssertionError("Repeated rendering changed page count, dimensions, mode or scale")
            state = pool_state(actual_render)
            verify_resources(rendered, state)
            ownership = rendered["ownership"]
            image_count = rendered["image_count"]
            del rendered
            gc.collect()
            observations.append(
                {
                    "iteration": iteration + 1,
                    "memory": memory,
                    "before": before,
                    "after_gc": process_tree(),
                    "pool": state,
                    "image_count": image_count,
                    "ownership": ownership,
                }
            )
        current_compute = backend_info()
        for field in ("backend", "protocol", "extension_sha256"):
            if compute.get(field) != current_compute.get(field):
                raise AssertionError(f"Compute identity changed during memory measurement: {field}")
        if source_identity(package) != config["source_identity"]:
            raise AssertionError("Source changed during memory measurement")
        report = {
            "scope": "input PDF document open, page count, all raster windows, PIL close, document close",
            "excluded_from_sampling": "input read/decode, pixel hashing, JSON validation, source hashing, explicit GC",
            "correctness_boundary": "counts/dimensions/modes/scales only; full pixels require separate pdf_input.py replay",
            "method": "isolated-worker-one-warmup-then-one-document-per-RSS-sample",
            "render_backend": actual_render,
            "compute": current_compute,
            "source_identity": config["source_identity"],
            "source_package": str(package),
            "input_identity": {key: config[key] for key in ("source_sha256", "payload_sha256", "source_encoding")},
            "window": config["window"],
            "processes": config["processes"],
            "iterations": config["iterations"],
            "images": shape,
            "warmed_pool": warmed_pool,
            "warmed_tree": warmed_tree,
            "observations": observations,
            "summary": {
                "median_tree_peak_rss_bytes": statistics.median(
                    item["memory"]["sampled_tree_peak_rss_bytes"] for item in observations
                ),
                "max_tree_peak_rss_bytes": max(item["memory"]["sampled_tree_peak_rss_bytes"] for item in observations),
                "post_gc_rss_delta_bytes": observations[-1]["after_gc"]["rss_bytes"] - warmed_tree["rss_bytes"],
                "worker_counts_stable": all(
                    item["pool"]["worker_count"] == warmed_pool["worker_count"] for item in observations
                ),
                "worker_pids_stable": all(item["pool"]["worker_pids"] == warmed_pool["worker_pids"] for item in observations),
                "resource_release_checks_passed": True,
                "interpretation": "finite replay evidence, not a proof of absence of all leaks or formal speed acceptance",
            },
            "started_at_utc": started_at,
        }
    finally:
        shutdown(actual_render)
    gc.collect()
    report["after_pool_shutdown"] = process_tree()
    report["pool_after_shutdown"] = pool_state(actual_render)
    states = [warmed_pool, *(item["pool"] for item in observations)]
    seen = {(item["pid"], item["create_time"]): item for state in states for item in state["worker_identities"]}
    report["os_worker_exit"] = verify_os_worker_exit([seen[key] for key in sorted(seen)])
    all_exited = report["os_worker_exit"]["all_observed_workers_exited"]
    registry_empty = report["pool_after_shutdown"]["worker_count"] == 0
    report["summary"]["resource_release_checks_passed"] = all_exited and registry_empty
    report["finished_at_utc"] = utc_now()
    # 失败也先持久化残留身份，不能仅抛异常而丢失泄漏证据。
    write_json(output, report)
    if not all_exited or not registry_empty:
        raise AssertionError("Observed OS render workers remain alive or could not be verified after shutdown")


def spawn_worker(config_path, output, config):
    """基线和候选串行启动独立进程，明确源码、计算与渲染后端及并发预算。"""
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": str(Path(config["source"]) / "src"),
            "DOCVORTEX_COMPUTE_BACKEND": config["compute_backend"],
            "DOCVORTEX_PDF_RENDER_BACKEND": config["render_backend"],
            "DOCVORTEX_PDF_RENDER_THREADS": str(config["processes"]),
            "LOGURU_LEVEL": "WARNING",
        }
    )
    subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker", str(config_path), "--output", str(output)],
        env=environment,
        check=True,
    )


def main():
    """冻结输入并分别验证基线与候选的连续处理，保留每次 RSS 而非仅报告平均值。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path)
    parser.add_argument("--baseline-source", type=Path)
    parser.add_argument("--candidate-source", type=Path)
    parser.add_argument("--baseline-render-backend", choices=("legacy", "session"), default="legacy")
    parser.add_argument("--candidate-render-backend", choices=("legacy", "session"), default="session")
    parser.add_argument("--backend", choices=("python", "rust"))
    parser.add_argument("--window", type=int, default=4)
    parser.add_argument("--processes", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--memory-budget-ratio", type=float, default=1.3)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        memory_worker(json.loads(args.worker.read_text()), args.output)
        return
    if not all((args.path, args.baseline_source, args.candidate_source, args.backend)):
        parser.error("path, both source roots and backend are required")
    if min(args.window, args.processes, args.iterations, args.timeout, args.memory_budget_ratio) <= 0 or args.output.exists():
        parser.error("positive limits and a new output directory are required")
    read_input, _, source_identity = benchmark_helpers()
    original = args.path.read_bytes()
    payload, source_hash, encoding = read_input(args.path)
    if hashlib.sha256(original).hexdigest() != source_hash:
        raise AssertionError("Input changed while creating the frozen copy")
    args.output.mkdir(parents=True)
    frozen = args.output / ("input.pdf.xor" if encoding == "xor" else "input.pdf")
    frozen.write_bytes(original)
    measurements = {}
    for label, source, render_backend in (
        ("baseline", args.baseline_source, args.baseline_render_backend),
        ("candidate", args.candidate_source, args.candidate_render_backend),
    ):
        config = {
            "source": str(source.resolve()),
            "source_identity": source_identity(source / "src/docvortex/__init__.py"),
            "path": str(args.path.resolve()),
            "frozen_input": str(frozen.resolve()),
            "source_sha256": source_hash,
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "source_encoding": encoding,
            "compute_backend": args.backend,
            "render_backend": render_backend,
            "window": args.window,
            "processes": args.processes,
            "iterations": args.iterations,
            "timeout": args.timeout,
        }
        config_path = args.output / f"{label}.config.json"
        output = args.output / f"{label}.json"
        write_json(config_path, config)
        spawn_worker(config_path, output, config)
        measurements[label] = json.loads(output.read_text())
    if measurements["baseline"]["images"] != measurements["candidate"]["images"]:
        raise AssertionError("Baseline and candidate render dimensions or scale differ")
    baseline = measurements["baseline"]["summary"]["median_tree_peak_rss_bytes"]
    candidate = measurements["candidate"]["summary"]["median_tree_peak_rss_bytes"]
    write_json(
        args.output / "report.json",
        {
            "scope": "isolated raster RSS and finite continuous-document resource replay; not full output parity",
            "measurements": measurements,
            "candidate_over_baseline_median_tree_peak_rss_ratio": candidate / baseline,
            "memory_budget_ratio": args.memory_budget_ratio,
            "median_peak_within_budget": candidate <= baseline * args.memory_budget_ratio,
            "max_peak_within_budget": measurements["candidate"]["summary"]["max_tree_peak_rss_bytes"]
            <= measurements["baseline"]["summary"]["max_tree_peak_rss_bytes"] * args.memory_budget_ratio,
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
    )


if __name__ == "__main__":
    main()
