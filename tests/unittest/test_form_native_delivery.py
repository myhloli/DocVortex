"""用真实 PDF 验证 Form 包装展开和完整图示保护，不依赖开发原件。"""

from io import BytesIO
import pytest
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject
from docvortex.document.pdf import PDFDocument
from docvortex.analyzers.native import PdfModel


def make_document(*, figure=False, wrapped=True, offset=0, font_size=11):
    """生成具有一致阅读行的普通正文或带外部图题的完整文本图示。"""
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    resources = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    content = []
    for i in range(12):
        content.append(
            f"BT /F1 {font_size} Tf 1 0 0 1 {60 + offset} {690 - i * 17} Tm (This independent paragraph explains the document workflow.) Tj ET"
        )
    stream = DecodedStreamObject()
    if wrapped:
        form = DecodedStreamObject()
        form.set_data("\n".join(content).encode())
        form.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Form"),
                NameObject("/BBox"): ArrayObject([FloatObject(v) for v in (40, 480, 570, 730)]),
                NameObject("/Resources"): resources,
            }
        )
        page_resources = DictionaryObject(resources)
        page_resources[NameObject("/XObject")] = DictionaryObject({NameObject("/Fm"): writer._add_object(form)})
        page[NameObject("/Resources")] = page_resources
        content = ["q /Fm Do Q"]
    else:
        page[NameObject("/Resources")] = resources
    if figure:
        content.append(f"BT /F1 {font_size} Tf 1 0 0 1 60 475 Tm (Figure 1. A complete visual example) Tj ET")
    stream.set_data("\n".join(content).encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def parse(data, diagnostics=True):
    """通过正常公开模型入口读取完整 PDF，并保留页级风险供断言。"""
    with PDFDocument(data) as pdf:
        model = PdfModel()
        pages = model.predict(pdf, collect_layout_diagnostics=diagnostics)
    return pages, model.layout_page_confidences


@pytest.mark.parametrize("offset,font_size", [(0, 11), (15, 11), (0, 9)])
def test_cropped_page_wrapper_delivers_body(offset, font_size):
    """带页边距的纯正文 Form 应展开，位置和字号改变仍交付完整正文。"""
    pages, confidences = parse(make_document(offset=offset, font_size=font_size))
    assert not any(b["type"] == "image" for b in pages[0])
    assert sum(str(b.get("content", "")).count("independent paragraph") for b in pages[0]) == 12
    assert confidences[0] == 1.0


def test_external_caption_preserves_whole_visual_unit():
    """图内有长段可提取文本也应保留图框，不能把文字数量当作拆图理由。"""
    pages, confidences = parse(make_document(figure=True))
    assert any(b["type"] == "image" for b in pages[0])
    assert confidences[0] == 1.0


def test_native_delivery_does_not_depend_on_diagnostics():
    """正常原生输出在诊断开关两侧一致，修复不隐藏在 PP 分流路径里。"""
    data = make_document()
    assert parse(data)[0] == parse(data, False)[0]
