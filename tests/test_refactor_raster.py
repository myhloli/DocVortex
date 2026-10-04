"""Guard behavioral boundaries for on-demand clipping and page resource lifecycles PDF."""

from __future__ import annotations

from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from unittest.mock import Mock

from copy import deepcopy
from io import BytesIO
from typing import Any

from PIL import Image
import pytest
from reportlab.pdfgen.canvas import Canvas

from docvortex import api
from docvortex.document.pdf import PDFDocument


def make_pdf(pages: int) -> bytes:
    """Construct the real PDF of the specified number of pages, so that the page extraction and the caller life cycle take the actual input path."""
    output = BytesIO()
    canvas = Canvas(output)
    for index in range(pages):
        canvas.drawString(60, 720, f"Page {index}")
        canvas.showPage()
    canvas.save()
    return output.getvalue()


def use_local_crop_worker(monkeypatch, raster):
    """Execute real cropping worker in the current process, only replace the underlying page image, and continue to check the image closing semantics."""
    from docvortex.document.pdf import images

    def load(data, prepared_pages, **options):
        """Connect the original test page image source to worker to simulate all cropping processes before process transmission."""

        def core(pdf_bytes, dpi, start, end, image_type):
            """Pass public dispatch parameters for the original assertion to check the actual page and configuration."""
            return raster(
                pdf_bytes,
                start_page_id=start,
                end_page_id=end,
                image_type=image_type,
                timeout=options.get("timeout"),
                threads=options.get("threads"),
            )

        with monkeypatch.context() as context:
            context.setattr(images, "load_images_from_pdf_core", core)
            return images._load_visual_crops_worker(
                data, 200, options["start_page_id"], options["end_page_id"], deepcopy(prepared_pages)
            )

    monkeypatch.setattr(images, "_load_visual_crops_from_pdf_bytes_range", load)


@pytest.mark.parametrize("page_range, expected_count", [("", 5), ("2-5", 4), ("r1", 1)])
def test_sparse_visual_pages_keep_physical_identity(
    monkeypatch: pytest.MonkeyPatch, page_range: str, expected_count: int
) -> None:
    """After page extraction, the image is cropped according to the physical index of the selected PDF, retaining the external page number mapping and releasing the image in time."""
    from docvortex.analyzers.native.models import PdfModel
    from docvortex.document.pdf import visuals

    raw_pages = [
        [
            {
                "type": "image" if index % 2 == 0 else "text",
                "bbox": [0, 0, 1, 1],
                "index": 0,
                "content": "" if index % 2 == 0 else [{"type": "text", "content": "body"}],
            }
        ]
        for index in range(expected_count)
    ]
    expected = deepcopy(raw_pages)
    reference_images = [{"img_pil": Image.new("RGB", (16, 16), (index * 30, 0, 0))} for index in range(expected_count)]
    visuals.attach_visual_block_images(expected, reference_images)
    for item in reference_images:
        item["img_pil"].close()
    calls, created = [], []

    def predict(_model: object, document: PDFDocument) -> list:
        """The real page extraction process is retained, and only deterministic visual blocks replace semantic analysis."""
        assert document.page_count == expected_count
        return deepcopy(raw_pages)

    def raster(
        _data: bytes,
        *,
        start_page_id: int,
        end_page_id: int,
        image_type: str,
        timeout: int | None = None,
        threads: int | None = None,
    ) -> list[dict[str, Any]]:
        """Check sparse scheduling and crop matching with images with physical page color."""
        calls.extend(range(start_page_id, end_page_id + 1))
        assert timeout is None and threads is None
        batch = [Image.new("RGB", (16, 16), (index * 30, 0, 0)) for index in range(start_page_id, end_page_id + 1)]
        created.extend(batch)
        return [{"img_pil": image} for image in batch]

    monkeypatch.setattr(PdfModel, "predict", predict)
    use_local_crop_worker(monkeypatch, raster)
    with PDFDocument(make_pdf(5)) as source:
        result = api.analyze(source, page_range=page_range)
        assert source.page_count == 5
    assert calls == list(range(0, expected_count, 2))
    assert result.model_json.pages == expected
    if page_range:
        assert result.model_json.page_index_map == ([4] if page_range == "r1" else [1, 2, 3, 4])
    for image in created:
        with pytest.raises(ValueError):
            image.getpixel((0, 0))


def test_text_only_pdf_does_not_start_raster_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """True text without visual block PDF The process pool should not be started."""
    from docvortex.document.pdf import images

    def forbidden(*args: object, **kwargs: object) -> object:
        """Any page image request indicates that the imageless path still performed an invalid rendering."""
        raise AssertionError("Text-only PDF must not rasterize")

    monkeypatch.setattr(images, "load_images_from_pdf_bytes_range", forbidden)
    monkeypatch.setattr(images, "_load_visual_crops_from_pdf_bytes_range", forbidden)
    assert api.parse(make_pdf(1)).middle_json.pages


def test_visual_windows_and_container_preparation() -> None:
    """Continuous cropping pages retain a 64-page window, and image_block folds and retains the source index before filtering pages."""
    from docvortex.document.pdf.visuals import _prepare_page_visual_blocks, _visual_page_ranges

    page = [{"type": "image_block", "bbox": [0, 0, 1, 1]}, {"type": "text", "bbox": [0.1, 0.1, 0.2, 0.2]}]
    prepared = _prepare_page_visual_blocks(page)
    assert len(page) == len(prepared) == 1
    assert page[0]["type"] == "image"
    pages = [prepared for _ in range(130)]
    pages[65] = []
    assert _visual_page_ranges(pages) == [(0, 63), (64, 64), (66, 127), (128, 129)]
    assert _visual_page_ranges([prepared] * 5, {index: 15 * 1024 * 1024 for index in range(5)}) == [(0, 1), (2, 3), (4, 4)]
    assert _visual_page_ranges([prepared] * 5, window_size=2) == [(0, 1), (2, 3), (4, 4)]
    assert _visual_page_ranges([prepared] * 2, {0: 40 * 1024 * 1024}) == [(0, 0), (1, 1)]


def test_crop_failure_releases_all_images(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the entire batch of cropping is abnormal, the returned page image will also be closed, and the PDFDocument held by the caller will be retained."""
    from docvortex.analyzers.native.models import PdfModel
    from docvortex.document.pdf import visuals

    image = Image.new("RGB", (8, 8))

    def predict(_model: object, _document: PDFDocument) -> list:
        """Arrange a visual block that needs to be cropped for the real input."""
        return [[{"type": "image", "bbox": [0, 0, 1, 1], "content": ""}]]

    def raster(*args: object, **kwargs: object) -> list:
        """Returns an image that can be checked for closed status."""
        return [{"img_pil": image}]

    def failure(*args: object, **kwargs: object) -> None:
        """Simulate cropping anomalies within the release protection area."""
        raise RuntimeError("crop failed")

    monkeypatch.setattr(PdfModel, "predict", predict)
    use_local_crop_worker(monkeypatch, raster)
    monkeypatch.setattr(visuals, "_attach_prepared_visual_block_images", failure)
    with PDFDocument(make_pdf(1)) as document:
        with pytest.raises(RuntimeError, match="crop failed"):
            api.analyze(document)
        assert document.page_count == 1
    with pytest.raises(ValueError):
        image.getpixel((0, 0))


@pytest.mark.parametrize("window_size", [0, -1, True, 1.5, "2"])
def test_public_visual_raster_rejects_invalid_window_before_mutation(window_size: Any) -> None:
    """Illegal windows must fail before the container is collapsed or the page map is accessed."""
    from docvortex.document.pdf.visuals import attach_visual_block_images_from_pdf

    pages = [[{"type": "image_block", "bbox": [0, 0, 1, 1]}]]
    expected = deepcopy(pages)
    with PDFDocument(make_pdf(1)) as document:
        with pytest.raises(ValueError, match="positive integer"):
            attach_visual_block_images_from_pdf(document, pages, window_size=window_size)
        assert document.page_count == 1
    assert pages == expected


def test_public_visual_raster_rejects_page_count_before_mutation() -> None:
    """The wrong number of pages cannot collapse the block or close the caller document early."""
    from docvortex.document.pdf.visuals import attach_visual_block_images_from_pdf

    pages = [[{"type": "image_block", "bbox": [0, 0, 1, 1]}]]
    with PDFDocument(make_pdf(2)) as document:
        with pytest.raises(ValueError, match="page count mismatch"):
            attach_visual_block_images_from_pdf(document, pages)
        assert document.page_count == 2
    assert pages[0][0]["type"] == "image_block"


@pytest.mark.parametrize("timeout,threads", [(None, None), (17, 2)])
def test_public_visual_raster_matches_eager_crops(
    monkeypatch: pytest.MonkeyPatch,
    timeout: int | None,
    threads: int | None,
) -> None:
    """Use the old full-page crop as a reference to verify sparse filter pages, containers, rotations, borders, and explicit configuration transparent transmission."""
    from docvortex.document.pdf import visuals

    pages = [
        [{"type": "text", "content": [{"type": "text", "content": "body"}]}],
        [{"type": "image_block", "bbox": [0, 0, 1, 1]}, {"type": "text", "bbox": [0.1, 0.1, 0.2, 0.2]}],
        [{"type": "chart", "bbox": [0.1, 0.1, 0.9, 0.9], "angle": 90}],
        [{"type": "table", "bbox": [-0.1, -0.1, 1.1, 1.1]}],
        [{"type": "equation", "bbox": [0.1, 0.1, 0.9, 0.9], "angle": 270}],
        [{"type": "image", "bbox": [0.5, 0.5, 0.5, 0.8]}],
        [],
    ]

    def page_image(index: int) -> Image.Image:
        """Create a page map with orientation and physical page color to prevent screenshots from being mispaged or being rotated."""
        image = Image.new("RGB", (32, 16), (index * 30, 20, 100))
        image.paste((0, 255, 0), (0, 0, 10, 8))
        return image

    expected = deepcopy(pages)
    reference = [{"img_pil": page_image(i)} for i in range(len(pages))]
    visuals.attach_visual_block_images(expected, reference)
    for item in reference:
        item["img_pil"].close()
    calls: list[tuple[int, int]] = []
    created: list[Image.Image] = []

    def raster(data: bytes, **options: Any) -> list[dict[str, Any]]:
        """Records the scheduling configuration and returns the same independent page map as the full page reference."""
        assert data.startswith(b"%PDF")
        assert options["timeout"] == timeout and options["threads"] == threads
        start, end = options["start_page_id"], options["end_page_id"]
        calls.append((start, end))
        batch = [page_image(i) for i in range(start, end + 1)]
        created.extend(batch)
        return [{"img_pil": image} for image in batch]

    use_local_crop_worker(monkeypatch, raster)
    with PDFDocument(make_pdf(len(pages))) as document:
        visuals.attach_visual_block_images_from_pdf(document, pages, window_size=2, timeout=timeout, threads=threads)
        assert document.page_count == len(pages)
    assert calls == [(1, 1), (2, 3), (4, 5)]
    assert pages == expected
    for image in created:
        with pytest.raises(ValueError):
            image.getpixel((0, 0))


@pytest.mark.parametrize("failure", ["render", "count"])
def test_public_visual_raster_failure_releases_completed_batches(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """When a subsequent batch fails, it neither leaks the page image of the previous batch nor takes over the external document life cycle."""
    from docvortex.document.pdf import visuals

    created: list[Image.Image] = []

    def raster(data: bytes, **options: Any) -> list[dict[str, Any]]:
        """The second batch of simulated loading fails or the wrong page number is checked, and all the acquired images can be released."""
        if options["start_page_id"] == 1 and failure == "render":
            raise RuntimeError("render failed")
        batch = [Image.new("RGB", (8, 8)) for _ in range(2 if options["start_page_id"] == 1 else 1)]
        created.extend(batch)
        return [{"img_pil": image} for image in batch]

    use_local_crop_worker(monkeypatch, raster)
    pages = [[{"type": "image", "bbox": [0, 0, 1, 1]}] for _ in range(2)]
    with PDFDocument(make_pdf(2)) as document:
        with pytest.raises((RuntimeError, ValueError), match="render failed|page count mismatch"):
            visuals.attach_visual_block_images_from_pdf(document, pages, window_size=1)
        assert document.page_count == 2
    for image in created:
        with pytest.raises(ValueError):
            image.getpixel((0, 0))


@pytest.mark.parametrize("failure", (TimeoutError, BrokenProcessPool, ValueError))
def test_encoded_crop_failure_preserves_pool_recovery(monkeypatch, failure):
    """The encoding material task reuses the original timeout and damage pool recycling rules, and ordinary calculation exceptions remain propagated as they are."""
    monkeypatch.setenv("DOCVORTEX_PDF_RENDER_BACKEND", "legacy")
    from docvortex.document.pdf import images

    executor = object()
    future = Future()
    if failure is not TimeoutError:
        future.set_exception(failure("worker failure"))
    monkeypatch.setattr(images, "_get_render_process_plan", lambda *args: (1, [(0, 0)]))
    monkeypatch.setattr(images, "_get_pdf_render_executor", lambda: executor)
    monkeypatch.setattr(images, "_submit_pdf_render_task", lambda *args: future)
    if failure is TimeoutError:
        monkeypatch.setattr(images, "wait", lambda *args, **kwargs: (set(), {future}))
    recycle = Mock()
    monkeypatch.setattr(images, "_recycle_pdf_render_executor", recycle)
    with pytest.raises(failure):
        images._load_visual_crops_from_pdf_bytes_range(b"pdf", [[]], 0, 0, 1, 1)
    if failure in (TimeoutError, BrokenProcessPool):
        recycle.assert_called_once_with(executor, terminate_processes=True)
    else:
        recycle.assert_not_called()


def test_encoded_crop_results_keep_page_order_and_pool(monkeypatch):
    """Pages are still backfilled when submitted in reverse order and repeated requests, and the encoded data is not sanitized or re-encoded by PIL."""
    monkeypatch.setenv("DOCVORTEX_PDF_RENDER_BACKEND", "legacy")
    from docvortex.document.pdf import images

    executor = object()
    submitted = []

    def submit(pool, worker, payload, dpi, start, end, prepared):
        """Returns a standalone completed Future, recording the actual worker and slice index passed in."""
        assert pool is executor and worker is images._load_visual_crops_worker
        submitted.append((start, prepared))
        future = Future()
        future.set_result([[(0, f"page-{start}")]])
        return future

    monkeypatch.setattr(images, "_get_render_process_plan", lambda *args: (2, [(1, 1), (0, 0)]))
    monkeypatch.setattr(images, "_get_pdf_render_executor", lambda: executor)
    monkeypatch.setattr(images, "_submit_pdf_render_task", submit)
    recycle = Mock()
    monkeypatch.setattr(images, "_recycle_pdf_render_executor", recycle)
    prepared = [[{"bbox": [0, 0, 1, 1]}], []]
    for _ in range(2):
        assert images._load_visual_crops_from_pdf_bytes_range(b"pdf", prepared, 0, 1, 1, 2) == [
            [(0, "page-0")],
            [(0, "page-1")],
        ]
    assert submitted == [(1, [[]]), (0, [prepared[0]])] * 2
    recycle.assert_not_called()
