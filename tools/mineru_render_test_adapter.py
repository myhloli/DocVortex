"""Connect the old page image avatar of the host scheduled test to the third round of cutting worker boundary without modifying the host code or assertions.

Only explicitly enabled via pytest -p tools.mineru_render_test_adapter; does not participate in production parsing.
"""

import pytest


@pytest.fixture(autouse=True)
def adapt_legacy_render_mock(request, monkeypatch):
    """Only injection points for old scheduled tests are adapted, real rendering is still covered by the engine's own regressions and full output verification."""
    if request.node.path.name != "test_flash_pdf_render_scheduling.py":
        return
    from docvortex.document.pdf import images, visuals

    legacy = images.load_images_from_pdf_bytes_range
    current = images._load_visual_crops_from_pdf_bytes_range

    def load(pdf_bytes, prepared_pages, start_page_id, end_page_id, timeout, threads):
        """Call the raster stand-in installed by the host test, keeping the crop, original block number, and image off assertions."""
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
