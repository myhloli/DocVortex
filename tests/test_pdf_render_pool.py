"""守卫持久 PDF 进程池的空闲复用与冷启动节流。"""


def test_idle_worker_does_not_pay_spawn_throttle() -> None:
    """持久池存在空闲 worker 时无需重复冷启动等待，探测不消耗空闲名额。"""
    import threading
    from types import SimpleNamespace
    from docvortex.document.pdf.images import _is_pdf_render_pool_still_spawning_workers

    idle = threading.Semaphore(1)
    executor = SimpleNamespace(_max_workers=3, _processes={1: object()}, _idle_worker_semaphore=idle)
    assert not _is_pdf_render_pool_still_spawning_workers(executor)
    assert idle.acquire(blocking=False)
    assert _is_pdf_render_pool_still_spawning_workers(executor)
    executor._processes = {index: object() for index in range(3)}
    assert not _is_pdf_render_pool_still_spawning_workers(executor)
