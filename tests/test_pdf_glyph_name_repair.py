"""真实嵌入字模、防同名资源混用以及通用 Adobe 名称映射回归。"""

from contextlib import closing
from ctypes import c_void_p, cast
from io import BytesIO

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
import pypdfium2 as pdfium
import pypdfium2.raw as raw
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from docvortex.document.pdf import PDFDocument
from docvortex.document.pdf.unicode_fallback import recover_page_unicode


def _dictionary(**values):
    """构造只使用真实 PDF 对象类型的小型资源字典。"""
    return DictionaryObject({NameObject("/" + key): value for key, value in values.items()})


def _stream(content, **values):
    """构造字体、字符映射或页面内容流。"""
    result = DecodedStreamObject()
    result.set_data(content)
    result.update(_dictionary(**values))
    return result


def _cff_fixture():
    """构造同名但不同原码语义的 Type1C 嵌入字体及一个未声明实际字模的反例。"""
    glyphs = [".notdef", "zero", "prime", "greaterequal", "beta_unknown"]
    builder = FontBuilder(1000, isTTF=False)
    builder.setupGlyphOrder(glyphs)
    builder.setupCharacterMap({0x30: "zero", 0x2032: "prime", 0x2265: "greaterequal"})
    shapes = {}
    for number, glyph in enumerate(glyphs):
        pen = T2CharStringPen(500, None)
        if number:
            pen.moveTo((15 * number, 0))
            pen.lineTo((50 * number, 650))
            pen.lineTo((100 * number, 675))
            pen.lineTo((105 * number, 0))
            pen.closePath()
        shapes[glyph] = pen.getCharString()
    font_name = "IndependentEncoding"
    builder.setupCFF(font_name, {"FullName": font_name, "FamilyName": font_name, "Weight": "Normal"}, shapes, {})
    data = BytesIO()
    with PdfWriter() as writer:
        name = NameObject("/" + font_name)
        contents = builder.font.getTableData("CFF ")
        font_file = writer._add_object(_stream(contents, Subtype=NameObject("/Type1C")))
        descriptor = writer._add_object(
            _dictionary(
                Type=NameObject("/FontDescriptor"),
                Flags=NumberObject(4),
                FontName=name,
                FontFile3=font_file,
                FontBBox=ArrayObject([NumberObject(n) for n in (0, 0, 500, 800)]),
                CapHeight=NumberObject(700),
                Ascent=NumberObject(800),
                Descent=NumberObject(-200),
                StemV=NumberObject(90),
                ItalicAngle=NumberObject(0),
            )
        )
        page = writer.add_blank_page(420, 180)
        map_to_q = b"begincmap\n1 begincodespacerange <00><ff> endcodespacerange\n1 beginbfchar <30><0051> endbfchar\nendcmap"
        fonts = {}
        operations = []
        for position, glyph in enumerate(("zero", "prime", "prime", "greaterequal", "beta_unknown", "delta")):
            font = _dictionary(
                Type=NameObject("/Font"),
                Subtype=NameObject("/Type1"),
                BaseFont=name,
                FontDescriptor=descriptor,
                Encoding=_dictionary(Differences=ArrayObject([NumberObject(48), NameObject("/" + glyph)])),
                FirstChar=NumberObject(48),
                LastChar=NumberObject(48),
                Widths=ArrayObject([NumberObject(500)]),
            )
            if position == 2:
                font[NameObject("/ToUnicode")] = writer._add_object(_stream(map_to_q))
            resource_name = "F" + str(position)
            fonts[resource_name] = writer._add_object(font)
            operations.append(f"BT /{resource_name} 14 Tf 1 0 0 1 {30 + 60 * position} 110 Tm <30> Tj ET")
        page.update(
            _dictionary(
                Resources=_dictionary(Font=_dictionary(**fonts)),
                Contents=writer._add_object(_stream("\n".join(operations).encode())),
            )
        )
        writer.write(data)
    return data.getvalue()


def test_cff_unknown_and_same_name_font_resources_keep_distinct_meanings(monkeypatch):
    """普通文本中的实际撇号恢复，真零、有效 Q、未知复合名称与无字模目录项保持。"""
    source = _cff_fixture()
    with PDFDocument(source) as pdf:
        with monkeypatch.context() as replace:
            replace.setattr(pdf, "_resolve_page_unicode", lambda *_: {})
            before = pdf.get_page_chars_with_geometry(0).chars
        actual = pdf.get_page_chars_with_geometry(0).chars
        snapshot = pdf[0].get_snapshot().text_geometry.chars
        assert [char["char"] for char in actual] == [char["char"] for char in snapshot]
        assert [(b["origin"], b["char"]) for a, b in zip(before, actual, strict=True) if a["char"] != b["char"]] == [
            ((90.0, 70.0), "′")
        ]
        assert all(
            tuple(a["bbox"]) == tuple(b["bbox"]) and a["origin"] == b["origin"] and a["char_idx"] == b["char_idx"]
            for a, b in zip(before, actual, strict=True)
        )
        assert {c["origin"][0]: c["char"] for c in actual if c["origin"][0] in {30, 90, 150, 210}} == {
            30: "0",
            90: "′",
            150: "Q",
            210: "≥",
        }
        assert pdf.bytes == source


def test_general_standard_agl_name_recovers_when_pdfium_returns_original_byte(monkeypatch):
    """模拟旧引擎对 ≥ 只返回 0，使用实际原码和 CFF 字形证明通用名称可修复。"""
    source = _cff_fixture()
    original_reader = PdfReader(BytesIO(source))
    with pdfium.PdfDocument(source) as pdf, closing(pdf[0]) as page, closing(page.get_textpage()) as text:
        get_unicode = raw.FPDFText_GetUnicode
        target = next(i for i in range(text.count_chars()) if get_unicode(text.raw, i) == ord("≥"))

        def original_fallback(handle, index):
            """只模拟原句柄在该字形处返回未经映射的原始字符码。"""
            if cast(handle, c_void_p).value == cast(text.raw, c_void_p).value and index == target:
                return ord("0")
            return get_unicode(handle, index)

        monkeypatch.setattr(raw, "FPDFText_GetUnicode", original_fallback)
        assert recover_page_unicode(text, [target], original_reader.pages[0], {}) == {target: ord("≥")}
