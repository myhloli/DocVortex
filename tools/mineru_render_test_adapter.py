"""把宿主调度测试的旧页图替身接到第三轮裁图 worker 边界，不修改宿主代码或断言。

仅通过 pytest -p tools.mineru_render_test_adapter 显式启用；不参与生产解析。
"""

import pytest


@pytest.fixture(autouse=True)
def adapt_legacy_render_mock(request, monkeypatch):
    """只适配旧调度测试的注入点，真实渲染仍由引擎自己的回归和完整输出验证覆盖。"""
    if request.node.path.name != "test_flash_pdf_render_scheduling.py":
        return
    from docvortex.document.pdf import images, visuals

    legacy = images.load_images_from_pdf_bytes_range
    current = images._load_visual_crops_from_pdf_bytes_range

    def load(pdf_bytes, prepared_pages, start_page_id, end_page_id, timeout, threads):
        """调用宿主测试安装的 raster 替身，保持裁图、原块编号和图片关闭断言。"""
        if images.load_images_from_pdf_bytes_range is legacy:
            return current(pdf_bytes, prepared_pages, start_page_id, end_page_id, timeout, threads)
        rendered = images.load_images_from_pdf_bytes_range(
            pdf_bytes,
            start_page_id=start_page_id,
            end_page_id=end_page_id,
            timeout=timeout,
            threads=threads,
        )
        try:
            visuals._attach_prepared_visual_block_images(prepared_pages, rendered, start_page_id)
            return [[(index, block.get("image_base64")) for index, block in page] for page in prepared_pages]
        finally:
            images._close_image_dicts(rendered)

    monkeypatch.setattr(images, "_load_visual_crops_from_pdf_bytes_range", load)
