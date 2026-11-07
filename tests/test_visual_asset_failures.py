"""素材生产失败在实际任务边界报告，不把任意 bbox 当作截图要求。"""

from unittest.mock import Mock

import pytest
from PIL import Image
from docvortex.document.pdf import visuals, images


def test_required_visual_reports_missing_page_image() -> None:
    """已选视觉素材缺少页图时失败，文本几何不要求素材。"""
    visuals.attach_visual_block_images([[{"type": "text", "bbox": [0, 0, 1, 1]}]], [{}])
    with pytest.raises(ValueError, match="page image missing: page=3"):
        visuals.attach_visual_block_images([[{"type": "image", "bbox": [0, 0, 1, 1]}]], [{}], page_start_index=3)


def test_encoding_failure_reports_page_and_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """编码异常不能只记日志后静默发布缺图结果。"""
    monkeypatch.setattr(visuals, "_encode_page_crop_as_jpeg_data_uri", Mock(side_effect=OSError("encode failed")))
    with Image.new("RGB", (20, 30)) as image:
        with pytest.raises(ValueError, match="page=4, block=0, type=chart.*encode failed"):
            visuals.attach_visual_block_images(
                [[{"type": "chart", "bbox": [0, 0, 1, 1]}]], [{"img_pil": image}], page_start_index=4
            )


def test_native_missing_crop_reports_generation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """按需渲染返回空素材时必须失败，不能交给 WebUI 补裁。"""
    monkeypatch.setattr(images, "get_pdf_render_backend", lambda: "legacy")
    monkeypatch.setattr(images, "_load_visual_crops_from_pdf_bytes_range", Mock(return_value=[[(0, None)]]))
    document = Mock(page_count=1, bytes=b"pdf")
    document.page_size.return_value = (100, 100)
    with pytest.raises(ValueError, match="PDF visual asset missing: page=0, block=0"):
        visuals.attach_visual_block_images_from_pdf(document, [[{"type": "table", "bbox": [0, 0, 1, 1]}]])


def test_native_crop_failure_keeps_page_block_and_cause() -> None:
    """原生裁图异常携带上下文并保留根因，不回退其他渲染路径。"""
    native = Mock()
    native.crop_bitmap_bgr.side_effect = RuntimeError("native crop failed")
    with pytest.raises(ValueError, match="crop failed: page=4, block=2") as failure:
        visuals._attach_owned_bitmap_crops(
            [(2, {"type": "table", "bbox": [0, 0, 1, 1]})], (b"pixels", 20, 30, 60, "BGR"), native, 4
        )
    assert isinstance(failure.value.__cause__, RuntimeError)
