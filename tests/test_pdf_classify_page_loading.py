"""classify 抽样页加载次数与图像覆盖统计的等价性（#19 回归）。

宽高比阶段用 FPDF_GetPageSizeByIndexF 免加载；文本采样与图像覆盖共用一次
页面加载；覆盖统计走深度 3 的类型过滤原生 walk，与旧 pypdfium2
get_objects(max_depth=3) 的可见图像集合一致。
"""

from __future__ import annotations

from importlib import import_module

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from docvortex.document.pdf import PDFDocument

classify = import_module("docvortex.document.pdf.classify")


def _pdf(objects: list[bytes]) -> bytes:
    """按对象号顺序写出带 xref 表的最小合法 PDF（对象 1 是 catalog）。"""
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
    """多页纯文本 PDF，每页正文足以通过 CHARS_THRESHOLD。"""
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
    """图像分布在页、一层、二层、三层嵌套 Form 的 PDF。

    深度 3 的 walk 只统计页与两层 Form 内的图像（Im0/Im1/Im2），
    三层 Form 内的 Im3 被排除，与旧 get_objects(max_depth=3) 一致。
    """
    image = b"\x80"

    def image_xobject() -> bytes:
        """生成一个最小的灰度图像 XObject。"""
        return (
            b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray /BitsPerComponent 8 /Length 1 >>\nstream\n"
            + image
            + b"\nendstream"
        )

    def form_xobject(resources_refs: bytes, content: bytes) -> bytes:
        """生成含指定资源与绘制指令的 Form XObject。"""
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
            image_xobject(),  # 5: Im0 页面级
            form_xobject(b"/Im1 7 0 R /F1 8 0 R", f0_content),  # 6: F0 一层
            image_xobject(),  # 7: Im1 深度 1
            form_xobject(b"/Im2 9 0 R /F2 10 0 R", f1_content),  # 8: F1 二层
            image_xobject(),  # 9: Im2 深度 2
            form_xobject(b"/Im3 11 0 R", f2_content),  # 10: F2 三层
            image_xobject(),  # 11: Im3 深度 3，必须被排除
        ]
    )


def _clipped_overlapping_images_pdf() -> bytes:
    """生成裁剪、重叠和缩放 Form 共存的纯图像页，检查旧面积统计语义。"""
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
    """沿用旧包装 walk 与原始 bounds，逐页返回未取阈值的覆盖率。"""
    page_bbox = page.get_bbox()
    page_area = abs((page_bbox[2] - page_bbox[0]) * (page_bbox[3] - page_bbox[1]))
    image_area = 0.0
    for page_object in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=3):
        left, bottom, right, top = page_object.get_bounds()
        image_area += max(0.0, right - left) * max(0.0, top - bottom)
    return min(image_area / page_area, 1.0) if page_area > 0 else 0.0


def _reference_coverage(pdf_doc: pdfium.PdfDocument, page_indices: list[int]) -> float:
    """旧实现：pypdfium2 对象包装 + get_objects(max_depth=3) 过滤。"""
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
    """统计 FPDF_LoadPage 路由的页面加载次数。"""
    counter = {"loads": 0}
    original = pdfium.PdfDocument.get_page

    def counting(self, index):
        """记录分类阶段实际触发的页面加载次数。"""
        counter["loads"] += 1
        return original(self, index)

    monkeypatch.setattr(pdfium.PdfDocument, "get_page", counting)
    return counter


def test_classify_loads_each_sampled_page_once(monkeypatch):
    """classify 全程每个抽样页只加载一次，而不是宽高比/文本/覆盖三阶段各一次。"""
    pages = 5
    counter = _count_page_loads(monkeypatch)

    with PDFDocument(_text_pages_pdf(pages)) as document:
        assert document.classify() == "txt"

    assert counter["loads"] == pages


def test_aspect_ratio_stage_loads_no_page(monkeypatch):
    """宽高比阶段直接按索引取尺寸，不触发任何页面加载。"""
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
    """嵌套 Form 各深度的图像覆盖统计与旧 pypdfium2 包装 walk 一致，深度 3 之外不计入。"""
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
    # Im0/Im1/Im2 各 200x200pt，合计约 0.25 页面占比；若深度 3 的 Im3
    # （600x700，近整页）被错误计入，覆盖率会被钳到 1.0。
    assert 0.15 < per_page < 0.5


def test_clipped_overlapping_form_images_keep_raw_area_semantics():
    """图像裁剪、重叠和 Form 变换不改变旧实现的原始面积求和规则。"""
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
