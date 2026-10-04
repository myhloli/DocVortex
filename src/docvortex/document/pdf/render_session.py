"""Optional directed PDF rendering session; input files are shared once and page images are transferred directly to the original bitmap."""

from __future__ import annotations

import atexit
import multiprocessing
import os
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import CancelledError
from contextlib import contextmanager
from pathlib import Path

from PIL import Image


_worker_budget = None
_worker_budget_lock = threading.RLock()
_worker_available = threading.Condition(_worker_budget_lock)
_worker_waiters = deque()
_idle_workers = []
_all_workers = []
_leased_workers = {}
_cached_workers = {}
_legacy_render_users = 0


def _get_worker_budget():
    """All new sessions share the lease quota to avoid filling up the process pool when processing multiple documents at the same time."""
    global _worker_budget
    from .images import _get_pdf_render_pool_capacity

    with _worker_budget_lock:
        if _worker_budget is None:
            _worker_budget = threading.BoundedSemaphore(_get_pdf_render_pool_capacity())
        return _worker_budget


def _dispose_worker(worker):
    """Terminate worker on failure or exit and make sure the shared input handle is closed before deleting the file."""
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


def _release_cached_input(worker, acknowledgement):
    """Record the input release in the pool lock; the cache ownership and the current task lease ownership can be different."""
    owner = _cached_workers.pop(worker, None)
    if owner is not None:
        owner.close_diagnostics.append(acknowledgement)
    _worker_available.notify_all()


def shutdown_pdf_render_sessions():
    """The persistent orientation pool is recycled when the process exits or is explicitly reset; active sessions detect disconnections and fail."""
    with _worker_available:
        workers = list(_all_workers)
        _all_workers.clear()
        _idle_workers.clear()
        for worker in workers:
            owner = _leased_workers.pop(worker, None)
            if owner is not None:
                owner._cancelled.set()
                owner._worker_budget.release()
            result = {"input_released": True, "pid": worker[0].pid, "worker_terminated": True}
            # An active pipe is read by the lessee, and another protocol command cannot be inserted into it by force-closing it.
            if owner is None:
                try:
                    worker[1].send((0, "shutdown", None))
                    if worker[1].poll(0.1):
                        _, status, value = worker[1].recv()
                        if status == "ack" and value.get("input_released") is True:
                            result = value
                except (OSError, EOFError):
                    pass
            _dispose_worker(worker)
            _release_cached_input(worker, result)
        _worker_available.notify_all()


def prepare_legacy_render_pool():
    """Release the idle processes in the new pool before switching back to the old entry, and prevent both backends from filling up the budget at the same time."""
    with _worker_budget_lock:
        if len(_all_workers) != len(_idle_workers):
            raise RuntimeError("Close active PDF render sessions before using the legacy render pool")
        shutdown_pdf_render_sessions()


@contextmanager
def legacy_render_scope():
    """The entire old task window registers concurrent occupancy, preventing new sessions from reclaiming the old pool before submit."""
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
    """Recycle the old free pool before renting the new pool; explicitly refuse superimposed concurrency when there are still tasks in the old pool."""
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


class _RenderPdfDocument:
    """Shared input files are held by PDFium to avoid the synchronization overhead of new files across process mmap."""

    def __init__(self, path):
        """The entry is loaded directly using the existing PDFium file, ownership persists until explicitly closed."""
        import pypdfium2 as pdfium
        from .pdfium import pdfium_guard

        self.document = None
        with pdfium_guard():
            self.document = pdfium.PdfDocument(Path(path))

    def close(self):
        """References are cleared only after the native document is successfully closed; failure results in session termination worker and file recycling."""
        from .pdfium import close_pdfium_document

        if self.document is not None:
            close_pdfium_document(self.document)
            self.document = None


def _render_session_worker(connection):
    """Serial processing directed protocol, document and file readers continue until explicitly closed or the connection is lost."""
    from .images import _initialize_pdf_render_worker, pdf_page_to_image
    from .pdfium import close_pdfium_child, pdfium_guard
    from ..._compute_backend import get_native
    from .raster import page_to_pixel_file, page_to_owned_bitmap
    from .visuals import _attach_prepared_visual_block_images, _attach_owned_bitmap_crops

    document = input_document = None
    try:
        _initialize_pdf_render_worker()
        native = get_native()
        while True:
            message = connection.recv()
            request_id, operation, payload = message
            try:
                if operation == "open":
                    if document is not None:
                        raise RuntimeError("Document already open")
                    input_document = _RenderPdfDocument(payload)
                    document = input_document.document
                    with pdfium_guard():
                        page_count = len(document)
                    result = {"page_count": page_count, "pid": os.getpid(), "document_opens": 1}
                elif operation in ("close", "shutdown"):
                    if input_document is not None:
                        input_document.close()
                        input_document = None
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
                                if crops is None and image_type == "pil_img":
                                    result.append(page_to_pixel_file(page, output_path, dpi))
                                    continue
                                if crops is not None and native is not None:
                                    bitmap = page_to_owned_bitmap(page, dpi)
                                else:
                                    bitmap = None
                                    item = pdf_page_to_image(page, dpi, image_type)
                            if bitmap is not None:
                                _attach_owned_bitmap_crops(crops, bitmap, native, page_id)
                                result.append([(index, block.get("image_base64")) for index, block in crops])
                                del bitmap
                                continue
                            image = item.get("img_pil")
                            if crops is not None:
                                _attach_prepared_visual_block_images([crops], [item], page_id)
                                result.append([(index, block.get("image_base64")) for index, block in crops])
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
            if input_document is not None:
                input_document.close()
        finally:
            connection.close()


class PDFRenderSession:
    """Holds document input, renting worker on a per-task basis; idle document cache can be immediately replaced by another session."""

    def __init__(self, pdf_bytes: bytes, *, threads: int | None = None, timeout: float | None = None):
        """Save the PDF input once and lazily create up to the existing concurrency budget number of workers."""
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
        """Refuse to commit page numbers of another document to an existing session, regular paths only compare object identities."""
        if pdf_bytes is not self._pdf_bytes and pdf_bytes != self._pdf_bytes:
            raise ValueError("PDF render session input does not match")

    def __enter__(self):
        """Returns a context session that can explicitly release resources."""
        return self

    def __exit__(self, *_):
        """Turn off all targeting when exiting context worker."""
        self.close()

    def _send(self, worker, operation, payload):
        """Send a command with an incrementing ID to prevent mismatched acknowledgments from being read."""
        self._request_id += 1
        worker[1].send((self._request_id, operation, payload))
        return self._request_id

    def _receive(self, worker, request_id, deadline, *, closing=False):
        """Short polling awaits confirmation while detecting deadlines, cancellations, and worker crashes."""
        process, connection = worker
        while True:
            if not closing and self._cancelled.is_set():
                raise CancelledError("PDF render session cancelled")
            with _worker_budget_lock:
                if _leased_workers.get(worker) is not self:
                    raise RuntimeError("PDF render worker lease was revoked")
            # First confirm that the process within the active lease is still alive to avoid consuming the last confirmation left before the crash.
            if not process.is_alive():
                raise RuntimeError(f"PDF render worker exited: {process.exitcode}")
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
                # The process must also remain alive after the write is acknowledged; worker should not exit itself after responding.
                if not process.is_alive():
                    raise RuntimeError(f"PDF render worker exited: {process.exitcode}")
                return value

    def _ensure_workers(self, count, deadline):
        """Press FIFO to rent the idle process, hit the document cache first, and switch input without occupying the pool lock."""
        context = multiprocessing.get_context("spawn")
        with _worker_available:
            _prepare_session_render_pool()
            _worker_waiters.append(self)
            try:
                while True:
                    if self._cancelled.is_set():
                        raise CancelledError("PDF render session cancelled")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("PDF render worker budget timed out")
                    if _worker_waiters[0] is self and self._worker_budget.acquire(blocking=False):
                        break
                    _worker_available.wait(remaining)
                # Only one slot is obtained first when there are other queued requests; tasks will not hold partial slots and wait for more.
                limit = 1 if len(_worker_waiters) > 1 else count
                for index in range(limit):
                    if index and not self._worker_budget.acquire(blocking=False):
                        break
                    worker = None
                    try:
                        # The old backend may start working during the waiting period, and the mutual exclusion boundary must be rechecked after the credit is obtained.
                        if index == 0:
                            _prepare_session_render_pool()
                        while _idle_workers:
                            worker = next(
                                (item for item in _idle_workers if _cached_workers.get(item) is self),
                                next((item for item in _idle_workers if item not in _cached_workers), _idle_workers[0]),
                            )
                            _idle_workers.remove(worker)
                            if worker[0].is_alive():
                                break
                            result = {"input_released": True, "pid": worker[0].pid, "worker_terminated": True}
                            _all_workers.remove(worker)
                            _dispose_worker(worker)
                            _release_cached_input(worker, result)
                            worker = None
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
                    except BaseException:
                        if worker not in self._workers:
                            self._worker_budget.release()
                        raise
            finally:
                _worker_waiters.remove(self)
                _worker_available.notify_all()

        pending_close = []
        pending_open = []
        for worker in self._workers:
            with _worker_budget_lock:
                if _leased_workers.get(worker) is not self:
                    raise RuntimeError("PDF render worker lease was revoked")
                cached = _cached_workers.get(worker)
            if cached is self:
                continue
            if cached is not None:
                pending_close.append((worker, self._send(worker, "close", None)))
            else:
                pending_open.append(worker)
        for worker, request_id in pending_close:
            result = self._receive(worker, request_id, deadline)
            if result.get("input_released") is not True:
                raise RuntimeError("PDF render close acknowledgement lacks input release")
            with _worker_available:
                _release_cached_input(worker, result)
            pending_open.append(worker)
        pending = []
        for worker in pending_open:
            with _worker_budget_lock:
                if _leased_workers.get(worker) is not self:
                    raise RuntimeError("PDF render worker lease was revoked")
                # Register before sending. If the opening fails, the input handle must be recycled first and then the temporary file must be deleted.
                _cached_workers[worker] = self
            pending.append((worker, self._send(worker, "open", str(self._input))))
        for worker, request_id in pending:
            result = self._receive(worker, request_id, deadline)
            self._page_count = result["page_count"]
            self.worker_diagnostics.append(result)

    def _release_workers(self):
        """When the task is completed, the usage rights are returned and the waiters are awakened, leaving a single-document cache that can be replaced at any time."""
        with _worker_available:
            for worker in self._workers:
                if _leased_workers.get(worker) is self:
                    del _leased_workers[worker]
                    _idle_workers.append(worker)
                    self._worker_budget.release()
            self._workers.clear()
            _worker_available.notify_all()

    @contextmanager
    def _task_lock(self, deadline):
        """Lock waits for concurrent requests in the same session are included in the deadline, and the timeout does not terminate another task."""
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
        """Return the page image or crop in the original order; the entire page pixels are not serialized through the pipeline."""
        from .images import MAX_PDF_RENDER_PROCESSES, _calculate_render_process_count

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
                # Full pages allocate lightweight session worker by at least four pages, still subject to the global budget; cropping retains the existing policy.
                if prepared_crops is None:
                    count = min(
                        MAX_PDF_RENDER_PROCESSES,
                        max(1, self.threads),
                        max(1, os.cpu_count() or 1),
                        max(1, (end_page_id - start_page_id + 1) // 4),
                    )
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
                            image = Image.frombytes(
                                value["mode"], value["size"], path.read_bytes(), "raw", value["raw_mode"], value["stride"], 1
                            )
                            collected.append(image)
                            path.unlink()
                            value = {"scale": value["scale"], "img_pil": image}
                        if self._cancelled.is_set():
                            raise CancelledError("PDF render session cancelled")
                        if time.monotonic() >= deadline:
                            raise TimeoutError("PDF render session timed out")
                        results.append((specification[0], value))
                self._release_workers()
                return [value for _, value in sorted(results)]
            except BaseException:
                for image in collected:
                    image.close()
                self._shutdown(graceful=False)
                raise

    def cancel(self):
        """Notifies that the current task is aborted and then closes the entire session rather than leaving a pending protocol message."""
        self._cancelled.set()
        self.close()

    def _shutdown(self, *, graceful):
        """Only cleans its own leases and caches; other sessions wait for their release confirmation when switching old inputs."""
        if self._closed:
            return
        self._closed = True
        with _worker_available:
            # The free cache is reserved atomically first to avoid being borrowed by other tasks when the input is closed.
            for worker in list(_idle_workers):
                if _cached_workers.get(worker) is self:
                    if not self._worker_budget.acquire(blocking=False):
                        raise RuntimeError("Idle PDF render worker lacks a budget token")
                    _idle_workers.remove(worker)
                    _leased_workers[worker] = self
                    self._workers.append(worker)
            workers = [worker for worker in self._workers if _leased_workers.get(worker) is self]
        deadline = time.monotonic() + 1.0
        pending = []
        reusable = {}
        if graceful:
            for worker in workers:
                try:
                    pending.append((worker, self._send(worker, "close", None)))
                except (OSError, EOFError):
                    pass
            for worker, request_id in pending:
                try:
                    result = self._receive(worker, request_id, deadline, closing=True)
                    if result.get("input_released") is not True:
                        raise RuntimeError("PDF render close acknowledgement lacks input release")
                    reusable[worker] = result
                except (OSError, RuntimeError, TimeoutError, ValueError):
                    pass
        with _worker_available:
            for worker in workers:
                if _leased_workers.get(worker) is not self:
                    continue
                if worker in reusable:
                    _idle_workers.append(worker)
                    result = reusable[worker]
                else:
                    result = {"input_released": True, "pid": worker[0].pid, "worker_terminated": True}
                    _all_workers.remove(worker)
                    _dispose_worker(worker)
                _release_cached_input(worker, result)
                del _leased_workers[worker]
                self._worker_budget.release()
            self._workers.clear()
            _worker_available.notify_all()
            # The borrower first closes the old input and then opens the new document without acquiring the old session lock; waiting will not form a session lock loop.
            while any(owner is self for owner in _cached_workers.values()):
                _worker_available.wait()
        self._directory.cleanup()
        self._pdf_bytes = None
        atexit.unregister(self.close)

    def close(self):
        """Cancel your own waiting or tasks and delete the file after confirming that this input is no longer held by any worker."""
        self._cancelled.set()
        with _worker_available:
            _worker_available.notify_all()
        with self._lock:
            self._shutdown(graceful=True)
