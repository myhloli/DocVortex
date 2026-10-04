"""Guard persistence PDF Idle multiplexing and cold start throttling of the process pool."""


def test_idle_worker_does_not_pay_spawn_throttle() -> None:
    """When there is idle worker in the persistent pool, there is no need to repeat the cold start wait, and the detection does not consume idle quota."""
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
