from __future__ import annotations

from concurrent.futures import ALL_COMPLETED, Future, ProcessPoolExecutor
from typing import Any, cast

import pytest
from loguru import logger

from docvortex.document.pdf import images as pdf_image_tools


class _FakeProcess:
    def __init__(self, pid: int | None, *, alive: bool, exit_code: int | None) -> None:
        """Initializes the parameters and process status recorded by the test avatar."""
        self.pid = pid
        self.exitcode = exit_code
        self._alive = alive

    def is_alive(self) -> bool:
        """Returns the survival status of the process specified by the test."""
        return self._alive


class _FakeExecutor:
    def __init__(self, processes: list[_FakeProcess]) -> None:
        """Initializes the parameters and process status recorded by the test avatar."""
        self._processes = {process.pid: process for process in processes}


def _capture_log_messages() -> tuple[list[str], int]:
    """Capture diagnostic logs of the render worker process for assertion use."""
    messages: list[str] = []
    handler_id = logger.add(lambda message: messages.append(str(message)), level="DEBUG", format="{message}")
    return messages, handler_id


def test_pdf_render_worker_exits_after_parent_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the parent process exits, the rendering worker process exits in time."""
    events: list[object] = []

    class _ParentProcess:
        def join(self) -> None:
            """Record the waiting action of the parent process and verify the exit monitoring logic."""
            events.append("parent.join")

    monkeypatch.setattr(pdf_image_tools.multiprocessing, "parent_process", _ParentProcess)
    monkeypatch.setattr(pdf_image_tools.os, "_exit", lambda code: events.append(("exit", code)))

    pdf_image_tools._exit_pdf_render_worker_when_parent_exits()

    assert events == ["parent.join", ("exit", 1)]


def test_pdf_render_worker_without_multiprocessing_parent_does_not_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not exit by mistake when the multi-process parent object does not exist."""
    monkeypatch.setattr(pdf_image_tools.multiprocessing, "parent_process", lambda: None)
    monkeypatch.setattr(pdf_image_tools.os, "_exit", lambda code: pytest.fail(f"unexpected exit {code}"))

    pdf_image_tools._exit_pdf_render_worker_when_parent_exits()


def test_pdf_render_executor_installs_parent_exit_watcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rendering worker Installs both parent process monitoring and fixed fonts, with monitoring taking precedence to cover initialization failures."""
    events: list[str] = []
    monkeypatch.setattr(pdf_image_tools, "_install_pdf_render_parent_exit_watcher", lambda: events.append("watcher"))
    monkeypatch.setattr(pdf_image_tools, "initialize_pdfium_runtime", lambda: events.append("fonts"))
    executor = pdf_image_tools._create_pdf_render_executor(max_workers=1)
    try:
        assert executor._initializer is pdf_image_tools._initialize_pdf_render_worker
        executor._initializer()
        assert events == ["watcher", "fonts"]
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def test_pdf_render_parent_exit_watcher_is_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """The parent process monitoring thread is started as a daemon thread."""
    events: list[object] = []

    class _Thread:
        def __init__(self, *, target: Any, name: str, daemon: bool) -> None:
            """Initializes the parameters and process status recorded by the test avatar."""
            events.append((target, name, daemon))

        def start(self) -> None:
            """Logging monitor thread starts without creating real threads."""
            events.append("start")

    monkeypatch.setattr(pdf_image_tools.threading, "Thread", _Thread)
    pdf_image_tools._install_pdf_render_parent_exit_watcher()

    assert events == [
        (
            pdf_image_tools._exit_pdf_render_worker_when_parent_exits,
            "docvortex-pdf-render-parent-exit-watcher",
            True,
        ),
        "start",
    ]


def test_pdf_render_future_states_cover_terminal_and_active_states() -> None:
    """Task status summary covers running, completed, canceled, and exceptions."""
    pending: Future[Any] = Future()
    running: Future[Any] = Future()
    assert running.set_running_or_notify_cancel()
    done: Future[Any] = Future()
    done.set_result([])
    cancelled: Future[Any] = Future()
    assert cancelled.cancel()

    states = pdf_image_tools._get_pdf_render_future_states(
        {
            pending: (0, 0),
            running: (1, 2),
            done: (3, 3),
            cancelled: (4, 5),
        }
    )

    assert states == [
        {"pages": "1-1", "state": "pending"},
        {"pages": "2-3", "state": "running"},
        {"pages": "4-4", "state": "done"},
        {"pages": "5-6", "state": "cancelled"},
    ]


def test_pdf_render_worker_states_include_pid_liveness_and_exit_code() -> None:
    """Work process status includes process number, survival status and exit code."""
    executor = cast(
        ProcessPoolExecutor,
        _FakeExecutor(
            [
                _FakeProcess(42, alive=False, exit_code=1),
                _FakeProcess(7, alive=True, exit_code=None),
            ]
        ),
    )

    assert pdf_image_tools._get_pdf_render_worker_states(executor) == [
        {"pid": 7, "alive": True, "exit_code": None},
        {"pid": 42, "alive": False, "exit_code": 1},
    ]


def test_pdf_render_worker_logs_start_and_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Output positionable logs when a rendering task starts and completes."""
    monkeypatch.setattr(pdf_image_tools, "load_images_from_pdf_core", lambda *args: [{"scale": 1.0}])
    messages, handler_id = _capture_log_messages()
    try:
        result = pdf_image_tools._load_images_from_pdf_worker(b"pdf", 200, 0, 0, pdf_image_tools.ImageType.PIL)
    finally:
        logger.remove(handler_id)

    assert result == [{"scale": 1.0}]
    assert any("PDF render worker started:" in message and "pages=1-1" in message for message in messages)
    assert any("PDF render worker completed:" in message and "images=1" in message for message in messages)


def test_pdf_render_worker_logs_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rendering exceptions contain diagnostic information for the worker task."""

    def fail_render(*args: Any) -> list[dict[str, Any]]:
        """Simulate rendering failures to verify exception diagnostic paths."""
        raise RuntimeError("render failed")

    monkeypatch.setattr(pdf_image_tools, "load_images_from_pdf_core", fail_render)
    messages, handler_id = _capture_log_messages()
    try:
        with pytest.raises(RuntimeError, match="render failed"):
            pdf_image_tools._load_images_from_pdf_worker(b"pdf", 200, 0, 0, pdf_image_tools.ImageType.PIL)
    finally:
        logger.remove(handler_id)

    assert any("PDF render worker failed:" in message and "pages=1-1" in message for message in messages)


def test_pdf_render_timeout_logs_future_and_worker_states(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retain task and worker process status and perform recycling when timeout occurs."""
    monkeypatch.setenv("DOCVORTEX_PDF_RENDER_BACKEND", "legacy")
    executor = cast(
        ProcessPoolExecutor,
        _FakeExecutor([_FakeProcess(314, alive=False, exit_code=9)]),
    )
    future: Future[Any] = Future()
    recycled: list[tuple[ProcessPoolExecutor | None, bool]] = []

    def fake_wait(
        futures: list[Future[Any]],
        timeout: int | None,
        return_when: str,
    ) -> tuple[set[Future[Any]], set[Future[Any]]]:
        """Simulate wait timeout and retain the set of observed tasks."""
        assert futures == [future]
        assert timeout == 0
        assert return_when == ALL_COMPLETED
        return set(), {future}

    def fake_recycle(candidate: ProcessPoolExecutor | None, *, terminate_processes: bool) -> None:
        """Record the work pool recycling call to avoid operating the real process."""
        recycled.append((candidate, terminate_processes))

    monkeypatch.setattr(pdf_image_tools, "_get_pdf_render_executor", lambda: executor)
    monkeypatch.setattr(pdf_image_tools, "_submit_pdf_render_task", lambda *args, **kwargs: future)
    monkeypatch.setattr(pdf_image_tools, "wait", fake_wait)
    monkeypatch.setattr(pdf_image_tools, "_recycle_pdf_render_executor", fake_recycle)

    messages, handler_id = _capture_log_messages()
    try:
        with pytest.raises(TimeoutError, match="timeout after 0s"):
            pdf_image_tools.load_images_from_pdf_bytes_range(b"pdf", start_page_id=0, end_page_id=0, timeout=0, threads=1)
    finally:
        logger.remove(handler_id)

    timeout_message = next(message for message in messages if "PDF image rendering timed out:" in message)
    assert "'state': 'pending'" in timeout_message
    assert "'pid': 314" in timeout_message
    assert "'alive': False" in timeout_message
    assert "'exit_code': 9" in timeout_message
    assert recycled == [(executor, True)]
