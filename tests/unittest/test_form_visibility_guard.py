"""可见性未确认时保持原始Form结果，不将隐藏图层展开或额外请求PP。"""

from io import BytesIO
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, BooleanObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject
from docvortex.document.pdf import PDFDocument
from docvortex.analyzers.native import PdfModel


def _menu_document(covered):
    """绘制同一个文字菜单，仅改变不透明底色与菜单的前后顺序。"""
    w = PdfWriter()
    page = w.add_blank_page(width=400, height=300)
    font = w._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    fontres = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    form = DecodedStreamObject()
    form.set_data(
        "\n".join(
            f"BT /F1 12 Tf 1 0 0 1 {40 if i < 2 else 220} {250 - i * 30} Tm ({word}) Tj ET"
            for i, word in enumerate(("Brand", "Logos", "Colors", "Screens"))
        ).encode()
    )
    form.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject(FloatObject(v) for v in (20, 120, 350, 280)),
            NameObject("/Resources"): fontres,
        }
    )
    resources = DictionaryObject(fontres)
    resources[NameObject("/XObject")] = DictionaryObject({NameObject("/Fm"): w._add_object(form)})
    resources[NameObject("/ExtGState")] = DictionaryObject(
        {
            NameObject("/Unused"): DictionaryObject(
                {NameObject("/OP"): BooleanObject(True), NameObject("/op"): BooleanObject(True)}
            )
        }
    )
    page[NameObject("/Resources")] = resources
    body = ["q /Fm Do Q", "q 0.1 0.3 0.8 rg 0 0 400 300 re f Q"]
    if not covered:
        body.reverse()
    body.append("BT /F1 16 Tf 1 0 0 1 170 70 Tm (VISIBLE TEXT) Tj ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(body).encode())
    page[NameObject("/Contents")] = w._add_object(stream)
    out = BytesIO()
    w.write(out)
    return out.getvalue()


def _predict(data):
    """走真实原生入口并返回页级未决证据，确认它与PP分流互相独立。"""
    with PDFDocument(data) as doc:
        model = PdfModel()
        pages = model.predict(doc, collect_layout_diagnostics=True)
        ambiguous = doc._extract_native_page(0).text_visibility_uncertain
    return pages[0], model.layout_page_confidences[0], ambiguous


def test_uncertain_overpaint_keeps_form_and_does_not_force_pp():
    """导航被覆盖但overprint资源让原可见性保守保留时，不把导航展开成正文。"""
    blocks, confidence, ambiguous = _predict(_menu_document(True))
    assert ambiguous
    assert any(block["type"] == "image" for block in blocks)
    assert not any("Brand" in str(block.get("content", "")) for block in blocks if block["type"] != "image")
    assert confidence == 1.0


def test_uncertain_resource_without_overpainting_can_expand_visible_menu():
    """资源表相同但底色先画时，真实可见菜单仍允许普通native展开。"""
    blocks, confidence, ambiguous = _predict(_menu_document(False))
    assert not ambiguous
    assert any("Brand" in str(block.get("content", "")) for block in blocks if block["type"] != "image")
    assert confidence == 1.0
