"""守卫按需 PDF 裁图和页面资源生命周期的行为边界。"""

from __future__ import annotations

from copy import deepcopy
from io import BytesIO

from PIL import Image
import pytest
from reportlab.pdfgen.canvas import Canvas

from docvortex import api
from docvortex.document.pdf import PDFDocument


def make_pdf(pages: int) -> bytes:
    """构造指定页数的真实 PDF，让抽页与调用者生命周期走实际输入路径。"""
    output = BytesIO()
    canvas = Canvas(output)
    for index in range(pages):
        canvas.drawString(60, 720, f"Page {index}")
        canvas.showPage()
    canvas.save()
    return output.getvalue()


@pytest.mark.parametrize("page_range, expected_count", [("", 5), ("2-5", 4), ("r1", 1)])
def test_sparse_visual_pages_keep_physical_identity(
    monkeypatch: pytest.MonkeyPatch, page_range: str, expected_count: int
) -> None:
    """抽页后按所选 PDF 的物理索引裁图，保留对外页号映射且及时释放图片。"""
    from docvortex.analyzers.native.models import PdfModel
    from docvortex.document.pdf import images, visuals

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
        """保留真实抽页流程，仅以确定性视觉块替代语义分析。"""
        assert document.page_count == expected_count
        return deepcopy(raw_pages)

    def raster(_data: bytes, *, start_page_id: int, end_page_id: int, image_type: str) -> list:
        """用带物理页颜色的图片检查稀疏调度和裁图匹配。"""
        calls.extend(range(start_page_id, end_page_id + 1))
        batch = [Image.new("RGB", (16, 16), (index * 30, 0, 0)) for index in range(start_page_id, end_page_id + 1)]
        created.extend(batch)
        return [{"img_pil": image} for image in batch]

    monkeypatch.setattr(PdfModel, "predict", predict)
    monkeypatch.setattr(images, "load_images_from_pdf_bytes_range", raster)
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
    """无视觉块的真实文本 PDF 不应启动进程池。"""
    from docvortex.document.pdf import images

    def forbidden(*args: object, **kwargs: object) -> object:
        """任何页图请求均表示无图路径仍执行了无效渲染。"""
        raise AssertionError("Text-only PDF must not rasterize")

    monkeypatch.setattr(images, "load_images_from_pdf_bytes_range", forbidden)
    assert api.parse(make_pdf(1)).middle_json.pages


def test_visual_windows_and_container_preparation() -> None:
    """连续需裁图页保留 64 页窗口，image_block 在筛页前折叠并保留来源索引。"""
    from docvortex.document.pdf.visuals import _prepare_page_visual_blocks, _visual_page_ranges

    page = [{"type": "image_block", "bbox": [0, 0, 1, 1]}, {"type": "text", "bbox": [0.1, 0.1, 0.2, 0.2]}]
    prepared = _prepare_page_visual_blocks(page)
    assert len(page) == len(prepared) == 1
    assert page[0]["type"] == "image"
    pages = [prepared for _ in range(130)]
    pages[65] = []
    assert _visual_page_ranges(pages) == [(0, 63), (64, 64), (66, 127), (128, 129)]
    assert _visual_page_ranges([prepared] * 5, {index: 15 * 1024 * 1024 for index in range(5)}) == [(0, 1), (2, 3), (4, 4)]


def test_crop_failure_releases_all_images(monkeypatch: pytest.MonkeyPatch) -> None:
    """整批裁图异常时也关闭已返回页图，并保留调用者持有的 PDFDocument。"""
    from docvortex.analyzers.native.models import PdfModel
    from docvortex.document.pdf import images, visuals

    image = Image.new("RGB", (8, 8))

    def predict(_model: object, _document: PDFDocument) -> list:
        """给真实输入安排一个需要裁图的视觉块。"""
        return [[{"type": "image", "bbox": [0, 0, 1, 1], "content": ""}]]

    def raster(*args: object, **kwargs: object) -> list:
        """返回可检查关闭状态的图片。"""
        return [{"img_pil": image}]

    def failure(*args: object, **kwargs: object) -> None:
        """在释放保护区域内模拟裁图异常。"""
        raise RuntimeError("crop failed")

    monkeypatch.setattr(PdfModel, "predict", predict)
    monkeypatch.setattr(images, "load_images_from_pdf_bytes_range", raster)
    monkeypatch.setattr(visuals, "_attach_prepared_visual_block_images", failure)
    with PDFDocument(make_pdf(1)) as document:
        with pytest.raises(RuntimeError, match="crop failed"):
            api.analyze(document)
        assert document.page_count == 1
    with pytest.raises(ValueError):
        image.getpixel((0, 0))
