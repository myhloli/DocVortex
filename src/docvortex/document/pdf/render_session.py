"""可选的定向 PDF 渲染会话；输入映射一次，页图通过临时文件传输。"""

from __future__ import annotations

import atexit
import ctypes
import mmap
import multiprocessing
import os
import tempfile
import threading
import time
from concurrent.futures import CancelledError
from contextlib import contextmanager
from pathlib import Path

from PIL import Image


_worker_budget = None
_worker_budget_lock = threading.RLock()
_idle_workers = []
_all_workers = []
_leased_workers = {}
_legacy_render_users = 0


def _get_worker_budget():
    """所有新会话共享租约额度，避免同时处理多文档时各自扩满进程池。"""
    global _worker_budget
    from .images import _get_pdf_render_pool_capacity

    with _worker_budget_lock:
        if _worker_budget is None:
            _worker_budget = threading.BoundedSemaphore(_get_pdf_render_pool_capacity())
        return _worker_budget


def _dispose_worker(worker):
    """终止故障或退出中的 worker，确保共享输入解除映射后再删除文件。"""
    process, connection = worker
    connection.close()
    process.join(timeout=0.1)
    if process.is_alive():
        process.terminate()
        process.join(timeout=0.1)
    if process.is_alive():
        process.kill()
        process.join(timeout=1.0)
    process.close()


def shutdown_pdf_render_sessions():
    """进程退出或显式重置时回收持久定向池；活动会话会检测断连并失败。"""
    with _worker_budget_lock:
        workers = list(_all_workers)
        _all_workers.clear()
        _idle_workers.clear()
        for worker in workers:
            owner = _leased_workers.pop(worker, None)
            if owner is not None:
                owner._cancelled.set()
                owner._worker_budget.release()
            try:
                worker[1].send((0, "shutdown", None))
                if worker[1].poll(0.1):
                    worker[1].recv()
            except (OSError, EOFError):
                pass
            _dispose_worker(worker)


def prepare_legacy_render_pool():
    """切回旧入口前释放新池的空闲进程，拒绝两种后端同时占满预算。"""
    with _worker_budget_lock:
        if len(_all_workers) != len(_idle_workers):
            raise RuntimeError("Close active PDF render sessions before using the legacy render pool")
        shutdown_pdf_render_sessions()


@contextmanager
def legacy_render_scope():
    """整个旧任务窗口登记并发占用，阻止新会话在 submit 前回收旧池。"""
    global _legacy_render_users
    with _worker_budget_lock:
        prepare_legacy_render_pool()
        _legacy_render_users += 1
    try:
        yield
    finally:
        with _worker_budget_lock:
            _legacy_render_users -= 1


def _prepare_session_render_pool():
    """租用新池前回收旧空闲池；旧池仍有任务时明确拒绝叠加并发。"""
    from . import images

    if _legacy_render_users:
        raise RuntimeError("Wait for legacy PDF render tasks before using render sessions")
    with images._pdf_render_executor_lock:
        executor = images._pdf_render_executor
        if executor is not None and getattr(executor, "_pending_work_items", {}):
            raise RuntimeError("Wait for legacy PDF render tasks before using render sessions")
    if executor is not None:
        images.shutdown_pdf_render_executor()


atexit.register(shutdown_pdf_render_sessions)


class _MappedPdfDocument:
    """独占 PDFium 文档及 mmap 导出视图，关闭过程不依赖循环 GC 的代次。"""

    def __init__(self, path):
        """只把非拥有的地址视图传给 ctypes/PDFium，映射导出由本对象单独持有。"""
        import pypdfium2 as pdfium
        from .pdfium import pdfium_guard

        self.document = None
        self.mapping = None
        self.source = None
        self._export = None
        try:
            self.source = open(path, "rb")
            self.mapping = mmap.mmap(self.source.fileno(), 0, access=mmap.ACCESS_COPY)
            array_type = ctypes.c_char * len(self.mapping)
            self._export = array_type.from_buffer(self.mapping)
            # ctypes.cast/参数转换可能给输入数组建立循环引用，直接传 from_buffer
            # 会让该循环持有 mmap 的导出指针。地址视图没有导出所有权，即便闭合的
            # PdfDocument/PdfPage Python 包装仍被引用，也不阻止确定性解除映射。
            borrowed = array_type.from_address(ctypes.addressof(self._export))
            with pdfium_guard():
                self.document = pdfium.PdfDocument(borrowed)
        except BaseException:
            self.close()
            raise

    def close(self):
        """先关闭全部 PDFium 原生句柄，再释放唯一导出视图、映射及源文件。"""
        from .pdfium import close_pdfium_document

        if self.document is not None:
            # 原生关闭失败时不得解除映射；让调用方报错并销毁整个 worker。
            close_pdfium_document(self.document)
            self.document = None
        self._export = None
        if self.mapping is not None:
            self.mapping.close()
            self.mapping = None
        if self.source is not None:
            self.source.close()
            self.source = None


def _render_session_worker(connection):
    """串行处理定向协议，文档与输入映射持续到显式关闭或连接断开。"""
    from .images import _initialize_pdf_render_worker, pdf_page_to_image
    from .pdfium import close_pdfium_child, pdfium_guard
    from .visuals import _attach_prepared_visual_block_images

    document = mapped_document = None
    try:
        _initialize_pdf_render_worker()
        while True:
            message = connection.recv()
            request_id, operation, payload = message
            try:
                if operation == "open":
                    if document is not None:
                        raise RuntimeError("Document already open")
                    mapped_document = _MappedPdfDocument(payload)
                    document = mapped_document.document
                    with pdfium_guard():
                        page_count = len(document)
                    result = {"page_count": page_count, "pid": os.getpid(), "document_opens": 1}
                elif operation in ("close", "shutdown"):
                    if mapped_document is not None:
                        mapped_document.close()
                        mapped_document = None
                    document = None
                    connection.send((request_id, "ack", {"input_released": True, "pid": os.getpid()}))
                    if operation == "shutdown":
                        break
                    continue
                elif operation == "task":
                    if document is None:
                        raise RuntimeError("Document not open")
                    result = []
                    for page_id, dpi, image_type, crops, output_path in payload:
                        page = image = None
                        try:
                            with pdfium_guard():
                                page = document[page_id]
                                item = pdf_page_to_image(page, dpi, image_type)
                            image = item.get("img_pil")
                            if crops is not None:
                                _attach_prepared_visual_block_images([crops], [item], page_id)
                                result.append([(index, block.get("image_base64")) for index, block in crops])
                            elif image is not None:
                                Path(output_path).write_bytes(image.tobytes())
                                result.append(
                                    {"scale": item["scale"], "mode": image.mode, "size": image.size, "path": output_path}
                                )
                            else:
                                result.append(item)
                        finally:
                            if image is not None:
                                image.close()
                            close_pdfium_child(page)
                            page = None
                else:
                    raise ValueError(f"Unknown render operation: {operation}")
                connection.send((request_id, "ack", result))
            except Exception as exc:
                connection.send((request_id, "error", (type(exc).__name__, str(exc))))
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        try:
            if mapped_document is not None:
                mapped_document.close()
        finally:
            connection.close()


class PDFRenderSession:
    """显式持有文档级 worker；返回的 PIL 像素不依赖会话生命周期。"""

    def __init__(self, pdf_bytes: bytes, *, threads: int | None = None, timeout: float | None = None):
        """保存一次 PDF 输入，惰性创建至多既有并发预算数量的 worker。"""
        from .images import MAX_PDF_RENDER_PROCESSES, get_load_images_threads, get_load_images_timeout

        if not pdf_bytes:
            raise ValueError("PDF input must not be empty")
        self._pdf_bytes = pdf_bytes
        self.timeout = get_load_images_timeout() if timeout is None else timeout
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        self.threads = min(MAX_PDF_RENDER_PROCESSES, max(1, threads or get_load_images_threads()), os.cpu_count() or 1)
        self._directory = tempfile.TemporaryDirectory(prefix="docvortex-render-")
        self._input = Path(self._directory.name) / "input.pdf"
        self._input.write_bytes(pdf_bytes)
        self._workers = []
        self._worker_budget = _get_worker_budget()
        self._request_id = 0
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._closed = False
        self._page_count = None
        self.worker_diagnostics = []
        self.close_diagnostics = []
        atexit.register(self.close)

    def validate_input(self, pdf_bytes):
        """拒绝把另一文档的页号提交到现有会话，常规路径仅比较对象身份。"""
        if pdf_bytes is not self._pdf_bytes and pdf_bytes != self._pdf_bytes:
            raise ValueError("PDF render session input does not match")

    def __enter__(self):
        """返回可显式释放资源的上下文会话。"""
        return self

    def __exit__(self, *_):
        """退出上下文时关闭全部定向 worker。"""
        self.close()

    def _send(self, worker, operation, payload):
        """发送具有递增标识的命令，防止读取错配的确认。"""
        self._request_id += 1
        worker[1].send((self._request_id, operation, payload))
        return self._request_id

    def _receive(self, worker, request_id, deadline, *, closing=False):
        """短轮询等待确认，同时检测截止时间、取消和 worker 崩溃。"""
        process, connection = worker
        while True:
            if not closing and self._cancelled.is_set():
                raise CancelledError("PDF render session cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("PDF render session timed out")
            if connection.poll(min(remaining, 0.05)):
                try:
                    received_id, status, value = connection.recv()
                except EOFError as exc:
                    raise RuntimeError("PDF render worker disconnected") from exc
                if received_id != request_id:
                    raise RuntimeError("PDF render protocol acknowledgement mismatch")
                if status != "ack":
                    raise RuntimeError(f"PDF render worker {value[0]}: {value[1]}")
                return value
            if not process.is_alive():
                raise RuntimeError(f"PDF render worker exited: {process.exitcode}")

    def _ensure_workers(self, count, deadline):
        """为文档惰性补充定向 worker，各进程只打开一次共享映射输入。"""
        if time.monotonic() >= deadline:
            raise TimeoutError("PDF render session timed out")
        context = multiprocessing.get_context("spawn")
        pending = []
        while len(self._workers) < count:
            if not self._worker_budget.acquire(blocking=False):
                if self._workers:
                    break
                if self._cancelled.is_set():
                    raise CancelledError("PDF render session cancelled")
                if time.monotonic() >= deadline:
                    raise TimeoutError("PDF render worker budget timed out")
                self._cancelled.wait(min(0.05, max(0, deadline - time.monotonic())))
                continue
            try:
                worker = None
                with _worker_budget_lock:
                    _prepare_session_render_pool()
                    while _idle_workers:
                        candidate = _idle_workers.pop()
                        if candidate[0].is_alive():
                            worker = candidate
                            break
                        _all_workers.remove(candidate)
                        _dispose_worker(candidate)
                    if worker is None:
                        parent, child = context.Pipe()
                        process = context.Process(target=_render_session_worker, args=(child,), daemon=True)
                        try:
                            process.start()
                        except BaseException:
                            parent.close()
                            child.close()
                            raise
                        child.close()
                        worker = (process, parent)
                        _all_workers.append(worker)
                    self._workers.append(worker)
                    _leased_workers[worker] = self
                pending.append((worker, self._send(worker, "open", str(self._input))))
            except BaseException:
                if worker is None or worker not in self._workers:
                    self._worker_budget.release()
                raise
        for worker, request_id in pending:
            result = self._receive(worker, request_id, deadline)
            self._page_count = result["page_count"]
            self.worker_diagnostics.append(result)

    @contextmanager
    def _task_lock(self, deadline):
        """同会话并发请求的锁等待计入截止时间，超时不终止另一个任务。"""
        acquired = self._lock.acquire(blocking=False)
        while not acquired:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("PDF render session lock timed out")
            acquired = self._lock.acquire(timeout=min(remaining, 0.05))
            if acquired:
                break
            if self._cancelled.is_set():
                raise CancelledError("PDF render session cancelled")
        try:
            yield
        finally:
            self._lock.release()

    def render(self, start_page_id=0, end_page_id=0, *, dpi=200, image_type="pil_img", timeout=None, prepared_crops=None):
        """按原顺序返回页图或裁图；整页像素不经过进程管道序列化。"""
        from .images import _calculate_render_process_count

        if end_page_id < start_page_id:
            return []
        if start_page_id < 0:
            raise ValueError("start_page_id must be nonnegative")
        if image_type not in ("pil_img", "base64_img"):
            raise ValueError("Unsupported image_type")
        if prepared_crops is not None and image_type != "pil_img":
            raise ValueError("Visual crops require PIL rendering")
        if prepared_crops is not None and len(prepared_crops) != end_page_id - start_page_id + 1:
            raise ValueError("Visual crop page count mismatch")
        duration = self.timeout if timeout is None else timeout
        if duration <= 0:
            raise ValueError("timeout must be positive")
        deadline = time.monotonic() + duration
        with self._task_lock(deadline):
            if self._closed:
                raise RuntimeError("PDF render session is closed")
            collected = []
            try:
                count = _calculate_render_process_count(end_page_id - start_page_id + 1, self.threads)
                self._ensure_workers(count, deadline)
                count = min(count, len(self._workers))
                end = min(end_page_id, self._page_count - 1)
                tasks = [[] for _ in range(count)]
                for index, page_id in enumerate(range(start_page_id, end + 1)):
                    crops = None if prepared_crops is None else prepared_crops[index]
                    tasks[index % count].append(
                        (page_id, dpi, image_type, crops, str(Path(self._directory.name) / f"page-{page_id}.pixels"))
                    )
                pending = [
                    (worker, self._send(worker, "task", task), task) for worker, task in zip(self._workers, tasks) if task
                ]
                results = []
                for worker, request_id, task in pending:
                    values = self._receive(worker, request_id, deadline)
                    if len(values) != len(task):
                        raise RuntimeError("PDF render task result count mismatch")
                    for specification, value in zip(task, values):
                        if prepared_crops is None and "path" in value:
                            path = Path(value["path"])
                            image = Image.frombytes(value["mode"], value["size"], path.read_bytes())
                            collected.append(image)
                            path.unlink()
                            value = {"scale": value["scale"], "img_pil": image}
                        if self._cancelled.is_set():
                            raise CancelledError("PDF render session cancelled")
                        if time.monotonic() >= deadline:
                            raise TimeoutError("PDF render session timed out")
                        results.append((specification[0], value))
                return [value for _, value in sorted(results)]
            except BaseException:
                for image in collected:
                    image.close()
                self._shutdown(graceful=False)
                raise

    def cancel(self):
        """通知当前任务中止，随后关闭整个会话而非留下待处理协议消息。"""
        self._cancelled.set()
        self.close()

    def _shutdown(self, *, graceful):
        """先尝试定向关闭确认，失败时终止进程，再删除所有共享文件。"""
        if self._closed:
            return
        self._closed = True
        deadline = time.monotonic() + 1.0
        pending = []
        reusable = []
        if graceful:
            for worker in self._workers:
                try:
                    pending.append((worker, self._send(worker, "close", None)))
                except (OSError, EOFError):
                    pass
            for worker, request_id in pending:
                try:
                    result = self._receive(worker, request_id, deadline, closing=True)
                    if result.get("input_released") is not True:
                        raise RuntimeError("PDF render close acknowledgement lacks input release")
                    self.close_diagnostics.append(result)
                    reusable.append(worker)
                except (OSError, RuntimeError, TimeoutError):
                    pass
        with _worker_budget_lock:
            for worker in self._workers:
                if worker in _all_workers:
                    if worker in reusable:
                        _idle_workers.append(worker)
                    else:
                        _all_workers.remove(worker)
                        _dispose_worker(worker)
                owner = _leased_workers.pop(worker, None)
                if owner is not None:
                    owner._worker_budget.release()
        self._workers.clear()
        self._directory.cleanup()
        self._pdf_bytes = None
        atexit.unregister(self.close)

    def close(self):
        """幂等关闭会话；活动任务先观察取消信号，不无限等待 PDFium 返回。"""
        self._cancelled.set()
        with self._lock:
            self._shutdown(graceful=True)
