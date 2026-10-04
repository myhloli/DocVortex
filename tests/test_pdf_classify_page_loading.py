"""classify Equivalence of sampled page load times and image coverage statistics (regression #19).

The aspect ratio stage uses FPDF_GetPageSizeByIndexF without loading; text sampling and image overlay are shared once
Page loads; override statistics go depth 3 type filter native walk, vs. old pypdfium2
The set of visible images for get_objects (max_depth=3) is consistent.
"""

from __future__ import annotations

from importlib import import_module

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from docvortex.document.pdf import PDFDocument

classify = import_module("docvortex.document.pdf.classify")


def _pdf(objects: list[bytes]) -> bytes:
    """Write the smallest legal PDF with the xref table in object number order (object 1 is catalog)."""
    out, offsets = bytearray(b"%PDF-1.7\n"), []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def _text_pages_pdf(pages: int, page_size: tuple[int, int] = (612, 792)) -> bytes:
    """Multiple pages of plain text PDF, each page of text is sufficient to pass CHARS_THRESHOLD."""
    line = b"BT /F1 12 Tf 72 720 Td (" + b"Plain text on a sampled classification page. " * 3 + b") Tj ET"
    kids = b" ".join(b"%d 0 R" % (4 + 2 * k) for k in range(pages))
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, pages),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for k in range(pages):
        objects += [
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>"
            % (page_size[0], page_size[1], 5 + 2 * k),
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(line), line),
        ]
    return _pdf(objects)


def _nested_forms_images_pdf() -> bytes:
    """The images are distributed in pages, one layer, two layers, and three layers of nested Form PDF.

    walk of depth 3 only counts images within the page and two layers of Form (Im0/Im1/Im2).
    Im3 within layer three Form is excluded, consistent with old get_objects (max_depth=3).
    """
    image = b"\x80"

    def image_xobject() -> bytes:
        """Generates a minimal grayscale image XObject."""
        return (
            b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray /BitsPerComponent 8 /Length 1 >>\nstream\n"
            + image
            + b"\nendstream"
        )

    def form_xobject(resources_refs: bytes, content: bytes) -> bytes:
        """Generate Form XObject containing specified resources and drawing instructions."""
        return (
            b"<< /Type /XObject /Subtype /Form /BBox [0 0 612 792] /Resources << /XObject << %s >> >> /Length %d >>\nstream\n%s\nendstream"
            % (
                resources_refs,
                len(content),
                content,
            )
        )

    page_content = b"q 200 0 0 200 50 500 cm /Im0 Do Q\nq 1 0 0 1 0 0 cm /F0 Do Q\n"
    f0_content = b"q 200 0 0 200 50 250 cm /Im1 Do Q\nq 1 0 0 1 0 0 cm /F1 Do Q\n"
    f1_content = b"q 200 0 0 200 250 500 cm /Im2 Do Q\nq 1 0 0 1 0 0 cm /F2 Do Q\n"
    f2_content = b"q 600 0 0 700 0 0 cm /Im3 Do Q\n"
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /XObject << /Im0 5 0 R /F0 6 0 R >> >> /Contents 4 0 R >>",
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(page_content), page_content),
            image_xobject(),  # 5: Im0 page level
            form_xobject(b"/Im1 7 0 R /F1 8 0 R", f0_content),  # 6: F0 first layer
            image_xobject(),  # 7: Im1 Depth 1
            form_xobject(b"/Im2 9 0 R /F2 10 0 R", f1_content),  # 8: F1 second floor
            image_xobject(),  # 9: Im2 Depth 2
            form_xobject(b"/Im3 11 0 R", f2_content),  # 10: F2 three layers
            image_xobject(),  # 11: Im3 depth 3, must be excluded
        ]
    )


def _clipped_overlapping_images_pdf() -> bytes:
    """Generate image-only pages for cropped, overlapped and scaled Form coexistence, checking old area statistics semantics."""
    image = b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray /BitsPerComponent 8 /Length 1 >>\nstream\n\x80\nendstream"
    content = (
        b"q 0 0 10 10 re W n 300 0 0 300 30 30 cm /Im Do Q\n"
        b"q 300 0 0 300 30 30 cm /Im Do Q\n"
        b"q 2 0 0 2 0 0 cm /F Do Q\n"
    )
    form_content = b"q 100 0 0 100 50 50 cm /Im Do Q\n"
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /XObject << /Im 5 0 R /F 6 0 R >> >> /Contents 4 0 R >>",
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
            image,
            b"<< /Type /XObject /Subtype /Form /BBox [0 0 612 792] /Resources << /XObject << /Im 5 0 R >> >> /Length %d >>\nstream\n%s\nendstream"
            % (len(form_content), form_content),
        ]
    )


def _reference_page_image_coverage_ratio(page: pdfium.PdfPage) -> float:
    """Use the old package walk and the original bounds to return unthresholded coverage page by page."""
    page_bbox = page.get_bbox()
    page_area = abs((page_bbox[2] - page_bbox[0]) * (page_bbox[3] - page_bbox[1]))
    image_area = 0.0
    for page_object in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=3):
        left, bottom, right, top = page_object.get_bounds()
        image_area += max(0.0, right - left) * max(0.0, top - bottom)
    return min(image_area / page_area, 1.0) if page_area > 0 else 0.0


def _reference_coverage(pdf_doc: pdfium.PdfDocument, page_indices: list[int]) -> float:
    """Old implementation: pypdfium2 object wrapping + get_objects(max_depth=3) filtering."""
    high_pages = 0
    for page_index in page_indices:
        page = pdf_doc[page_index]
        try:
            coverage_ratio = _reference_page_image_coverage_ratio(page)
            if coverage_ratio >= classify.HIGH_IMAGE_COVERAGE_THRESHOLD:
                high_pages += 1
        finally:
            page.close()
    return high_pages / len(page_indices) if page_indices else 0.0


def _count_page_loads(monkeypatch):
    """Count the number of page loads for the FPDF_LoadPage route."""
    counter = {"loads": 0}
    original = pdfium.PdfDocument.get_page

    def counting(self, index):
        """Record the number of page loads actually triggered during the classification phase."""
        counter["loads"] += 1
        return original(self, index)

    monkeypatch.setattr(pdfium.PdfDocument, "get_page", counting)
    return counter


def test_classify_loads_each_sampled_page_once(monkeypatch):
    """classify only loads each sampling page once in the whole process, instead of once for each of the three stages of aspect ratio/text/overlay."""
    pages = 5
    counter = _count_page_loads(monkeypatch)

    with PDFDocument(_text_pages_pdf(pages)) as document:
        assert document.classify() == "txt"

    assert counter["loads"] == pages


def test_aspect_ratio_stage_loads_no_page(monkeypatch):
    """The aspect ratio stage directly retrieves the size by index without triggering any page loading."""
    counter = _count_page_loads(monkeypatch)
    data = _text_pages_pdf(3, page_size=(612, 792))

    with pdfium.PdfDocument(data) as pdf_doc:
        index, ratio = classify.get_extreme_aspect_ratio_page_pdfium(pdf_doc, [0, 1, 2])
    assert index is None and ratio is None
    assert counter["loads"] == 0

    wide = _text_pages_pdf(2, page_size=(9000, 600))
    with pdfium.PdfDocument(wide) as pdf_doc:
        index, ratio = classify.get_extreme_aspect_ratio_page_pdfium(pdf_doc, [0, 1])
    assert index == 0 and ratio > 10.0
    assert counter["loads"] == 0


def test_native_image_coverage_matches_wrapper_walk():
    """Image coverage statistics at each depth for nested Form are consistent with the old pypdfium2 wrapper walk, depths beyond depth 3 are not counted."""
    data = _nested_forms_images_pdf()
    with pdfium.PdfDocument(data) as pdf_doc:
        reference = _reference_coverage(pdf_doc, [0])
        actual = classify.get_high_image_coverage_ratio_pdfium(pdf_doc, [0])
        page = pdf_doc[0]
        try:
            per_page = classify._page_image_coverage_ratio(page)
        finally:
            page.close()

    assert reference == actual
    # Im0/Im1/Im2 each 200x200pt, the total page share is about 0.25; if Im3 of depth 3
    # (600x700, nearly a full page) is incorrectly counted and the coverage is clamped to 1.0.
    assert 0.15 < per_page < 0.5


def test_clipped_overlapping_form_images_keep_raw_area_semantics():
    """Image cropping, overlapping, and Form transformations do not change the original area summation rules of the old implementation."""
    data = _clipped_overlapping_images_pdf()
    with pdfium.PdfDocument(data) as pdf_doc:
        page = pdf_doc[0]
        try:
            reference = _reference_page_image_coverage_ratio(page)
            actual = classify._page_image_coverage_ratio(page)
        finally:
            page.close()
    assert actual == reference
    assert 0.1 < actual <= 1.0
    with PDFDocument(data) as document:
        assert document.classify() == "ocr"
