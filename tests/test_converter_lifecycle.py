"""验证职责拆分后转换器状态、类型入口及失败重试的隔离性。"""

from __future__ import annotations

from copy import deepcopy
from io import BytesIO

import pytest


@pytest.mark.parametrize("suffix", ["docx", "pptx"])
def test_converter_reuse_after_failure_matches_fresh_instance(suffix: str) -> None:
    """不同文档及失败重试使用独立状态，不修改之前交付的页面列表。"""
    if suffix == "docx":
        from docx import Document

        from docvortex.analyzers.native.office.docx.docx_converter import DocxConverter as Converter

        def payload(text: str) -> bytes:
            """生成包含单段正文的独立 DOCX。"""
            document = Document()
            document.add_paragraph(text)
            stream = BytesIO()
            document.save(stream)
            return stream.getvalue()
    else:
        from pptx import Presentation

        from docvortex.analyzers.native.office.pptx.pptx_converter import PptxConverter as Converter

        def payload(text: str) -> bytes:
            """生成包含一个标题页的独立 PPTX。"""
            document = Presentation()
            slide = document.slides.add_slide(document.slide_layouts[0])
            slide.shapes.title.text = text
            stream = BytesIO()
            document.save(stream)
            return stream.getvalue()

    converter = Converter()
    converter.convert(BytesIO(payload("first document")))
    first_pages = converter.pages
    first_snapshot = deepcopy(first_pages)
    with pytest.raises(Exception):
        converter.convert(BytesIO(b"invalid package"))
    second = payload("second document")
    converter.convert(BytesIO(second))
    fresh = Converter()
    fresh.convert(BytesIO(second))
    assert converter.pages == fresh.pages
    assert converter.pages is not first_pages
    assert first_pages == first_snapshot


def test_pdf_contract_identity_survives_module_split() -> None:
    """原 PDF 模块的类型仍是唯一对象，已存在的 pickle 数据可以继续读取。"""
    import pickle

    from docvortex.document.pdf import _document as document
    from docvortex.document.pdf import native_contracts

    assert document.PDFDrawingLine is native_contracts.PDFDrawingLine
    value = document.PDFDrawingLine((0, 0), (10, 0), (0, 0, 10, 0), 1, "horizontal")
    assert pickle.loads(pickle.dumps(value)) == value
