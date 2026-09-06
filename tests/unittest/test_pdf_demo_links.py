"""真实 PDF 超链接从原生分析到各格式输出的回归。"""

from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from docgale.analyzers.native import PdfModel
from docgale.document.pdf.document import PDFDocument
from docgale.postprocess.pages import model_json_to_pages
from docgale.render import render_docx, render_html, render_markdown, render_structured_content
from docgale.schema import MiddleJson, ModelJson, PageInfo
from _span_test_utils import inline_text, inline_urls


def test_demo1_pdf_link_reaches_model_middle_and_all_renderers() -> None:
    """验证真实 demo1 URI Link 贯穿 model、MiddleJson 和四类 renderer。"""

    pdf_path = Path(__file__).parents[2] / "demo/pdfs/demo1.pdf"
    with PDFDocument(str(pdf_path)) as document:
        model_list = PdfModel().predict(document)

    target = "http://www.elsevier.com/locate/jhydrol"
    label = "www.elsevier.com/locate/jhydrol"
    assert any(
        inline_text(block.get("content")) == label and inline_urls(block.get("content")) == [target] for block in model_list[0]
    )

    middle = MiddleJson(
        pages=model_json_to_pages(ModelJson(pages=model_list, page_index_map=[], file_suffix="pdf")),
        is_full_document=True,
        file_suffix="pdf",
    )
    link_block = next(
        block
        for block in middle.pages[0].blocks
        if inline_text(getattr(block, "content", None)) == label and inline_urls(getattr(block, "content", None)) == [target]
    )
    link_middle = MiddleJson(
        pages=[PageInfo(page_idx=0, blocks=[link_block])],
        is_full_document=True,
        file_suffix="pdf",
    )
    assert f"[{label}]({target})" in render_markdown(link_middle)
    assert f'href="{target}"' in render_html(link_middle, standalone=False)
    relationships = ZipFile(BytesIO(render_docx(link_middle))).read("word/_rels/document.xml.rels").decode("utf-8")
    assert target in relationships
    assert f"[{label}]({target})" in str(render_structured_content(link_middle))
