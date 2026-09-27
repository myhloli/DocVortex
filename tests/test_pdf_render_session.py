"""真实 PDF 验证定向渲染会话的复用、像素所有权及异常清理。"""

from copy import deepcopy
from io import BytesIO
from pathlib import Path
import threading

import pytest
from reportlab.pdfgen.canvas import Canvas

from docvortex.document.pdf.images import load_images_from_pdf_core, load_images_from_pdf_bytes_range
from docvortex.document.pdf.render_session import PDFRenderSession, shutdown_pdf_render_sessions
from docvortex.document.pdf.visuals import _attach_prepared_visual_block_images


@pytest.fixture
def session_pdf():
    """生成具有不同尺寸、透明图形和稀疏视觉页的多页真实 PDF。"""
    output = BytesIO()
    canvas = Canvas(output, pagesize=(120, 180))
    for index in range(5):
        canvas.setPageSize((120 + 10 * index, 180))
        canvas.drawString(10, 160, f"Page {index}")
        if index in (0, 4):
            canvas.setFillColorRGB(0.2, 0.4, 0.7)
            canvas.setFillAlpha(0.4)
            canvas.rect(10, 20, 50, 70, fill=1)
        canvas.showPage()
    canvas.save()
    return output.getvalue()


def test_session_sparse_windows_reuse_document_and_return_owned_pixels(session_pdf):
    """稀疏窗口只打开一次文档，像素逐字节等价且关闭后仍可使用。"""
    images = []
    with PDFRenderSession(session_pdf, threads=1) as session:
        directory = Path(session._directory.name)
        for page_id in (0, 4, 0):
            actual = load_images_from_pdf_bytes_range(session_pdf, start_page_id=page_id, end_page_id=page_id, session=session)
            expected = load_images_from_pdf_core(session_pdf, start_page_id=page_id, end_page_id=page_id)
            assert actual[0]["scale"] == expected[0]["scale"]
            assert actual[0]["img_pil"].tobytes() == expected[0]["img_pil"].tobytes()
            expected[0]["img_pil"].close()
            images.extend(actual)
        assert len(session.worker_diagnostics) == 1
        assert session.worker_diagnostics[0]["document_opens"] == 1
        assert not list(directory.glob("*.pixels"))
    assert not directory.exists()
    for item in images:
        assert item["img_pil"].getpixel((0, 0))
        item["img_pil"].close()
    session.close()
    with pytest.raises(RuntimeError, match="closed"):
        session.render()


def test_session_multiple_crops_match_existing_encoder(session_pdf):
    """同页多个不同角度裁图复用一次栅格化并保持原编码结果。"""
    specs = [
        [
            (2, {"bbox": [0.1, 0.1, 0.5, 0.5], "angle": 0, "type": "image"}),
            (4, {"bbox": [0.3, 0.2, 0.8, 0.7], "angle": 90, "type": "image"}),
        ]
    ]
    expected = deepcopy(specs)
    images = load_images_from_pdf_core(session_pdf)
    try:
        _attach_prepared_visual_block_images(expected, images[:1])
    finally:
        for image in images:
            image["img_pil"].close()
    with PDFRenderSession(session_pdf) as session:
        actual = session.render(prepared_crops=specs)
    assert actual == [[(index, block["image_base64"]) for index, block in expected[0]]]


def test_session_timeout_releases_input_and_workers(session_pdf, monkeypatch):
    """在真实 worker 已打开文档后注入过期截止时间，确定性验证超时及资源回收。"""
    session = PDFRenderSession(session_pdf)
    session.render(image_type="base64_img")
    assert session._workers
    receive = session._receive

    def expired_response(worker, request_id, deadline, **kwargs):
        """只改变响应等待截止时间，保留真实管道、超时检测和失败清理路径。"""
        return receive(worker, request_id, float("-inf"), **kwargs)

    monkeypatch.setattr(session, "_receive", expired_response)
    directory = Path(session._directory.name)
    with pytest.raises(TimeoutError):
        session.render()
    assert session._closed
    assert not session._workers
    assert not directory.exists()


def test_session_worker_crash_releases_resources(session_pdf):
    """定向进程异常退出会使会话失败关闭，不尝试静默换后端。"""
    session = PDFRenderSession(session_pdf)
    session.render(image_type="base64_img")
    process = session._workers[0][0]
    process.terminate()
    process.join(5)
    directory = Path(session._directory.name)
    with pytest.raises((RuntimeError, OSError, EOFError)):
        session.render()
    assert session._closed
    assert not directory.exists()


def test_session_rejects_mismatched_document(session_pdf):
    """错误文档不得误用已有会话返回另一份 PDF 的像素。"""
    with PDFRenderSession(session_pdf) as session:
        with pytest.raises(ValueError, match="does not match"):
            load_images_from_pdf_bytes_range(b"different PDF", session=session)
        assert not session._workers


def test_session_cancel_interrupts_wait_and_reclaims_workers(session_pdf, monkeypatch):
    """并发取消唤醒等待任务且不会因关闭锁产生死锁。"""
    from concurrent.futures import CancelledError

    session = PDFRenderSession(session_pdf)
    ready = threading.Event()
    errors = []
    original = session._receive

    def await_cancel(worker, request_id, deadline, **kwargs):
        """把响应等待固定在可取消点，避免依赖机器渲染速度。"""
        ready.set()
        session._cancelled.wait(10)
        return original(worker, request_id, deadline, **kwargs)

    def render_task():
        """记录后台任务异常以验证取消类型与资源清理。"""
        try:
            session.render()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(session, "_receive", await_cancel)
    thread = threading.Thread(target=render_task)
    thread.start()
    assert ready.wait(10)
    session.cancel()
    thread.join(5)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], CancelledError)
    assert session._closed and not session._workers


def test_sessions_reuse_worker_after_document_close_ack(session_pdf):
    """跨文档保持同一进程，关闭确认后输入映射可删除并重开另一文档。"""
    shutdown_pdf_render_sessions()
    try:
        with PDFRenderSession(session_pdf) as first:
            first.render(image_type="base64_img")
            first_pid = first.worker_diagnostics[0]["pid"]
            input_path = first._input
        assert not input_path.exists()
        assert first.close_diagnostics == [{"input_released": True, "pid": first_pid}]
        output = BytesIO()
        canvas = Canvas(output, pagesize=(80, 90))
        canvas.drawString(5, 20, "Second")
        canvas.save()
        with PDFRenderSession(output.getvalue()) as second:
            result = second.render(dpi=72)
            assert second.worker_diagnostics[0]["pid"] == first_pid
            assert result[0]["img_pil"].size == (80, 90)
            result[0]["img_pil"].close()
    finally:
        shutdown_pdf_render_sessions()


def test_concurrent_documents_wait_for_lease_and_reuse_worker(session_pdf, monkeypatch):
    """两份文档争用一个槽位时等待关闭确认后复用进程，不共用活动句柄。"""
    from docvortex.document.pdf import render_session as module

    shutdown_pdf_render_sessions()
    budget = threading.BoundedSemaphore(1)
    waiting = threading.Event()
    acquire = budget.acquire

    def observe_acquire(*args, **kwargs):
        """记录租约真正耗尽，避免依赖睡眠推测后台线程已进入等待。"""
        result = acquire(*args, **kwargs)
        if not result:
            waiting.set()
        return result

    monkeypatch.setattr(budget, "acquire", observe_acquire)
    monkeypatch.setattr(module, "_worker_budget", budget)
    first = PDFRenderSession(session_pdf)
    second = PDFRenderSession(session_pdf)
    errors = []

    def render_second():
        """后台申请第二份文档的独占租约并记录异常。"""
        try:
            second.render(image_type="base64_img", timeout=10)
        except BaseException as exc:
            errors.append(exc)

    try:
        first.render(image_type="base64_img")
        first_pid = first.worker_diagnostics[0]["pid"]
        thread = threading.Thread(target=render_second)
        thread.start()
        assert waiting.wait(5)
        assert not second._workers
        first.close()
        thread.join(10)
        assert not thread.is_alive() and not errors
        assert second.worker_diagnostics[0]["pid"] == first_pid
        assert first.close_diagnostics[0]["input_released"]
    finally:
        first.close()
        second.close()
        shutdown_pdf_render_sessions()


def test_lease_timeout_does_not_destroy_another_document(session_pdf, monkeypatch):
    """等待槽位超时仅回收自己的会话，另一文档仍可正常渲染。"""
    from docvortex.document.pdf import render_session as module

    shutdown_pdf_render_sessions()
    monkeypatch.setattr(module, "_worker_budget", threading.BoundedSemaphore(1))
    try:
        with PDFRenderSession(session_pdf) as first:
            first.render(image_type="base64_img")
            with PDFRenderSession(session_pdf, timeout=0.05) as blocked:
                with pytest.raises(TimeoutError, match="budget"):
                    blocked.render()
                assert blocked._closed
            assert first.render(image_type="base64_img")[0]["img_base64"]
            assert len(first.worker_diagnostics) == 1
    finally:
        shutdown_pdf_render_sessions()


def test_forced_pool_shutdown_releases_active_lease(session_pdf, monkeypatch):
    """全池强制关闭释放活跃租约，新会话无需等待旧对象再次调用 close。"""
    from concurrent.futures import CancelledError
    from docvortex.document.pdf import render_session as module

    shutdown_pdf_render_sessions()
    monkeypatch.setattr(module, "_worker_budget", threading.BoundedSemaphore(1))
    first = PDFRenderSession(session_pdf)
    try:
        first.render(image_type="base64_img")
        first_pid = first.worker_diagnostics[0]["pid"]
        shutdown_pdf_render_sessions()
        with PDFRenderSession(session_pdf) as second:
            second.render(image_type="base64_img")
            assert second.worker_diagnostics[0]["pid"] != first_pid
        with pytest.raises((CancelledError, OSError, RuntimeError)):
            first.render()
    finally:
        first.close()
        shutdown_pdf_render_sessions()


def test_two_workers_preserve_page_order_and_reuse_handles():
    """两个定向进程处理交错页号后恢复原页序，第二个窗口不重开文档。"""
    output = BytesIO()
    canvas = Canvas(output, pagesize=(40, 50))
    for index in range(60):
        canvas.setPageSize((40 + index % 3, 50))
        canvas.drawString(2, 20, str(index))
        canvas.showPage()
    canvas.save()
    pdf = output.getvalue()
    expected = load_images_from_pdf_core(pdf, dpi=72, image_type="base64_img")
    with PDFRenderSession(pdf, threads=2) as session:
        actual = session.render(0, 59, dpi=72, image_type="base64_img")
        assert actual == expected
        assert len(session.worker_diagnostics) == 2
        assert session.render(59, 59, dpi=72, image_type="base64_img") == expected[59:]
        assert len(session.worker_diagnostics) == 2


def test_mode_switch_rejects_overlap_without_cancelling_active_task(session_pdf, monkeypatch):
    """新旧模式重叠只拒绝新请求，既有活动会话或旧窗口仍保持可用。"""
    from docvortex.document.pdf.render_session import legacy_render_scope

    monkeypatch.setenv("DOCVORTEX_PDF_RENDER_BACKEND", "legacy")

    with PDFRenderSession(session_pdf) as active:
        active.render(image_type="base64_img")
        with pytest.raises(RuntimeError, match="active PDF render sessions"):
            load_images_from_pdf_bytes_range(session_pdf)
        assert active.render(image_type="base64_img")[0]["img_base64"]
    with legacy_render_scope():
        with PDFRenderSession(session_pdf) as blocked:
            with pytest.raises(RuntimeError, match="legacy PDF render tasks"):
                blocked.render()
    with PDFRenderSession(session_pdf) as resumed:
        assert resumed.render(image_type="base64_img")[0]["img_base64"]


def test_busy_session_lock_timeout_preserves_session(session_pdf):
    """同会话等待串行锁超时不破坏持锁方的文档状态。"""
    with PDFRenderSession(session_pdf) as session:
        with session._lock:
            with pytest.raises(TimeoutError, match="lock"):
                session.render(timeout=0.01)
        assert not session._closed
        assert session.render(image_type="base64_img")[0]["img_base64"]


def test_document_owns_one_session_and_releases_on_close(session_pdf):
    """文档惰性会话跨窗口保持身份，文档关闭后归还租约并删除输入。"""
    from docvortex.document.pdf import PDFDocument, PDFRenderSession as exported

    assert exported is PDFRenderSession
    document = PDFDocument(session_pdf)
    assert document._render_session is None
    session = document.get_render_session(threads=1)
    assert document.get_render_session() is session
    for page in (0, 4):
        image = session.render(page, page)[0]["img_pil"]
        image.close()
    assert len(session.worker_diagnostics) == 1
    document.close()
    assert session._closed and not session._input.exists()
    document.close()


def test_session_backend_routes_bytes_and_visual_windows(session_pdf, monkeypatch):
    """统一选择器让 bytes 与稀疏视觉窗口均绕过旧池且复用定向进程。"""
    from docvortex.document.pdf import PDFDocument, images
    from docvortex.document.pdf.visuals import attach_visual_block_images_from_pdf

    def forbidden(*args, **kwargs):
        """session 配置下任何旧池请求都视为路由错误。"""
        raise AssertionError("legacy pool must not run")

    monkeypatch.setenv("DOCVORTEX_PDF_RENDER_BACKEND", "session")
    monkeypatch.setattr(images, "_load_pdf_render_tasks", forbidden)
    for page in (0, 4):
        result = images.load_images_from_pdf_bytes_range(session_pdf, start_page_id=page, end_page_id=page)
        result[0]["img_pil"].close()
    with PDFDocument(session_pdf) as document:
        models = [
            [{"type": "image", "bbox": [0.1, 0.1, 0.5, 0.5]}],
            [],
            [],
            [],
            [{"type": "image", "bbox": [0.1, 0.1, 0.5, 0.5]}],
        ]
        attach_visual_block_images_from_pdf(document, models, window_size=1)
        session = document.get_render_session()
        assert len(session.worker_diagnostics) == 1
        assert models[0][0]["image_base64"] and models[4][0]["image_base64"]
    assert session.close_diagnostics[0]["input_released"]


def test_unknown_render_backend_is_not_silently_ignored(session_pdf, monkeypatch):
    """拼错后端选择必须明确失败，不能误用旧路径产出误导基准。"""
    monkeypatch.setenv("DOCVORTEX_PDF_RENDER_BACKEND", "sessions")
    with pytest.raises(ValueError, match="legacy or session"):
        load_images_from_pdf_bytes_range(session_pdf)


def test_document_close_preserves_original_error_after_session_cleanup_failure(session_pdf):
    """会话清理失败仍关闭 PDFium 句柄，不能覆盖解析抛出的原始异常。"""
    from types import SimpleNamespace
    from docvortex.document.pdf import PDFDocument

    def fail_close():
        """模拟关闭会话时发生的次生资源错误。"""
        raise RuntimeError("secondary close failure")

    document = PDFDocument(session_pdf)
    assert document.page_count == 5
    native = document._pdf_doc_opened
    document._render_session = SimpleNamespace(close=fail_close)
    with pytest.raises(KeyError, match="original parse failure"):
        with document:
            raise KeyError("original parse failure")
    assert document._pdf_doc_opened is None
    assert not native.raw


def test_render_document_closes_with_old_generation_wrappers(session_pdf, tmp_path, monkeypatch):
    """旧代文档与页面包装仍被引用时，文件也必须确定释放而不调用 GC。"""
    import gc

    from docvortex.document.pdf.pdfium import pdfium_guard
    from docvortex.document.pdf.render_session import _RenderPdfDocument

    source_path = tmp_path / "mapped.pdf"
    source_path.write_bytes(session_pdf)
    owner = _RenderPdfDocument(source_path)
    native = owner.document
    # 主动保留闭合包装及其自循环，覆盖自动 GC 已把它们提升到旧代的情形。
    native._retained_cycle = (native,)
    with pdfium_guard():
        page = native[0]
        assert page.get_size()[0] > 0
    gc.collect(2)

    def forbid_collection(*args, **kwargs):
        """禁止用强制全代 GC 避开导出视图所有权缺陷。"""
        raise AssertionError("mapped input cleanup must not depend on GC")

    enabled = gc.isenabled()
    gc.disable()
    try:
        with monkeypatch.context() as context:
            context.setattr(gc, "collect", forbid_collection)
            owner.close()
            owner.close()
        assert not native.raw and not page.raw
        source_path.unlink()
        assert not source_path.exists()
    finally:
        owner.close()
        native._retained_cycle = None
        if enabled:
            gc.enable()


def test_render_document_keeps_input_when_native_close_fails(session_pdf, tmp_path, monkeypatch):
    """PDFium 尚未确认关闭时保留原生所有权，恢复关闭后才释放文件。"""
    from docvortex.document.pdf import pdfium as runtime
    from docvortex.document.pdf.render_session import _RenderPdfDocument

    source_path = tmp_path / "native-close-error.pdf"
    source_path.write_bytes(session_pdf)
    owner = _RenderPdfDocument(source_path)

    def fail_close(document):
        """模拟 native close 在句柄仍存活时抛错。"""
        raise RuntimeError("native close failed")

    try:
        with monkeypatch.context() as context:
            context.setattr(runtime, "close_pdfium_document", fail_close)
            with pytest.raises(RuntimeError, match="native close failed"):
                owner.close()
            assert owner.document.raw
            assert source_path.exists()
    finally:
        owner.close()
    assert owner.document is None
    source_path.unlink()


def test_render_document_open_failure_releases_file(tmp_path):
    """真实 PDFium 拒绝输入后文件仍可删除，失败的加载不留下文件句柄。"""
    import pypdfium2
    from docvortex.document.pdf.render_session import _RenderPdfDocument

    source_path = tmp_path / "rejected.pdf"
    source_path.write_bytes(b"invalid pdf bytes")
    with pytest.raises(pypdfium2.PdfiumError):
        _RenderPdfDocument(source_path)
    source_path.unlink()


@pytest.mark.parametrize("pixel_format", [1, 2, 3, 4])
@pytest.mark.parametrize("reverse", [False, True])
def test_raw_pixel_transport_preserves_format_stride_and_ownership(session_pdf, tmp_path, monkeypatch, pixel_format, reverse):
    """灰度、三通道及带透明通道的原始缓冲均保持像素、行跨度和关闭后所有权。"""
    import mmap
    import pypdfium2 as pdfium
    from PIL import Image
    from docvortex.document.pdf.raster import page_to_image, page_to_pixel_file

    with pdfium.PdfDocument(session_pdf) as document:
        page = document[0]
        render = page.render

        def selected_format(**kwargs):
            """显式覆盖真实 PDFium 位图格式及旋转裁剪，验证传输不依赖默认三通道。"""
            return render(**kwargs, force_bitmap_format=pixel_format, rev_byteorder=reverse, rotation=90, crop=(1, 2, 3, 4))

        monkeypatch.setattr(page, "render", selected_format)
        expected, scale = page_to_image(page, dpi=97)
        path = tmp_path / "pixels.raw"
        metadata = page_to_pixel_file(page, path, dpi=97)
        with path.open("rb") as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as pixels:
            actual = Image.frombytes(
                metadata["mode"], metadata["size"], pixels, "raw", metadata["raw_mode"], metadata["stride"], 1
            )
        path.unlink()
        page.close()
    try:
        assert metadata["scale"] == scale
        assert actual.mode == expected.mode
        assert actual.size == expected.size
        assert actual.tobytes() == expected.tobytes()
        actual.putpixel((0, 0), expected.getpixel((0, 0)))
    finally:
        actual.close()
        expected.close()


def test_parallel_session_preserves_order_pixels_and_crop_budget(monkeypatch):
    """真实多进程覆盖旋转与透明页：像素、页序和关闭后所有权不变，裁图不增加 worker。"""
    import os
    from docvortex.document.pdf import render_session

    shutdown_pdf_render_sessions()
    monkeypatch.setenv("DOCVORTEX_PDF_RENDER_THREADS", "3")
    monkeypatch.setattr(render_session, "_worker_budget", threading.BoundedSemaphore(3))
    buffer = BytesIO()
    canvas = Canvas(buffer)
    for index in range(12):
        canvas.setPageSize((120 + index, 180))
        canvas.setPageRotation((0, 90, 180, 270)[index % 4])
        canvas.setFillAlpha(0.4)
        canvas.setFillColorRGB(index / 12, 0.3, 0.7)
        canvas.rect(10, 20, 40, 70, fill=1)
        canvas.drawString(10, 110, f"Order {index}")
        canvas.showPage()
    canvas.save()
    payload = buffer.getvalue()
    expected = load_images_from_pdf_core(payload, dpi=97, start_page_id=0, end_page_id=11)
    actual = []
    try:
        with PDFRenderSession(payload, threads=3) as session:
            directory = Path(session._directory.name)
            actual = session.render(0, 11, dpi=97)
            assert len(session.worker_diagnostics) == min(3, max(1, os.cpu_count() or 1))
            assert all(item["document_opens"] == 1 for item in session.worker_diagnostics)
            assert not list(directory.glob("*.pixels"))
        assert not directory.exists()
        for first, second in zip(expected, actual, strict=True):
            assert first["scale"] == second["scale"]
            assert first["img_pil"].size == second["img_pil"].size
            assert first["img_pil"].tobytes() == second["img_pil"].tobytes()
            second["img_pil"].putpixel((0, 0), (1, 2, 3))
        with PDFRenderSession(payload, threads=3) as crops:
            assert crops.render(0, 11, prepared_crops=[[] for _ in range(12)]) == [[] for _ in range(12)]
            assert len(crops.worker_diagnostics) == 1
    finally:
        for item in [*expected, *actual]:
            item["img_pil"].close()
        shutdown_pdf_render_sessions()


def test_owned_bitmap_survives_pdfium_close_without_pil(session_pdf, monkeypatch):
    """直接取图不经过 PIL，位图及文档关闭后独立字节仍可裁剪编码。"""
    import pypdfium2 as pdfium
    from docvortex._compute_backend import get_native
    from docvortex.document.pdf.pdfium import pdfium_guard
    from docvortex.document.pdf.raster import page_to_owned_bitmap
    from docvortex.document.pdf.visuals import _attach_owned_bitmap_crops

    native = get_native()
    if native is None:
        pytest.skip("Python reference backend")

    def forbidden_pil(*args, **kwargs):
        """新直裁路径若物化整页 PIL，立即使测试失败。"""
        raise AssertionError("unexpected full-page PIL materialization")

    monkeypatch.setattr(pdfium.PdfBitmap, "to_pil", forbidden_pil)
    with pdfium_guard():
        with pdfium.PdfDocument(session_pdf) as document:
            page = document[0]
            try:
                bitmap = page_to_owned_bitmap(page)
            finally:
                page.close()
    blocks = [(0, {"bbox": [0.1, 0.1, 0.8, 0.9], "angle": 270})]
    _attach_owned_bitmap_crops(blocks, bitmap, native, 0)
    assert blocks[0][1]["image_base64"].startswith("data:image/jpeg;base64,")


def test_default_auto_render_backend_and_explicit_modes(monkeypatch):
    """默认及显式 auto 均启用会话，显式 session 和 legacy 保留选择能力。"""
    from docvortex.document.pdf.images import get_pdf_render_backend

    monkeypatch.delenv("DOCVORTEX_PDF_RENDER_BACKEND", raising=False)
    assert get_pdf_render_backend() == "session"
    for mode in ("auto", "session"):
        monkeypatch.setenv("DOCVORTEX_PDF_RENDER_BACKEND", mode)
        assert get_pdf_render_backend() == "session"
    monkeypatch.setenv("DOCVORTEX_PDF_RENDER_BACKEND", "legacy")
    assert get_pdf_render_backend() == "legacy"
