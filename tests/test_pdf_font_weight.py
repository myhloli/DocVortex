"""覆盖异常 StemV 的真实字体、两条字符读取路径及公开粗体输出。"""

from collections import Counter
import ctypes
from io import BytesIO
from pathlib import Path
from typing import Any, Iterator

from fontTools.ttLib import TTFont as FontToolsFont
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject
import pypdfium2.raw as raw
import pytest
import reportlab
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from docvortex import parse
from docvortex.document.pdf import PDFDocument

SOURCE = Path(__file__).parent / "fixtures/pdf_font_weight/source.pdf"


def _spans(value: Any) -> Iterator[dict]:
    """递归读取自然语言片段，覆盖正文、标题和嵌套结构。"""
    if isinstance(value, dict):
        if value.get("type") == "text" and isinstance(value.get("content"), str):
            yield value
        for item in value.values():
            yield from _spans(item)
    elif isinstance(value, list):
        for item in value:
            yield from _spans(item)


def _opaque_font_pdf(stem: int, alias: str, *, explicit_weight: int | None = None, embedded: bool = True) -> bytes:
    """用同名字体资源构造普通字与真粗体，改变 StemV 而不改变嵌入字形。"""
    output = BytesIO()
    canvas = Canvas(output, pagesize=(500, 300))
    text = canvas.beginText(40, 240)
    for filename, phrase in (("Vera.ttf", "Ordinary words "), ("VeraBd.ttf", "emphasized words")):
        name = f"WeightRegression-{filename}"
        pdfmetrics.registerFont(TTFont(name, str(Path(reportlab.__file__).parent / "fonts" / filename)))
        text.setFont(name, 12)
        text.textOut(phrase)
    canvas.drawText(text)
    canvas.save()
    writer = PdfWriter(clone_from=BytesIO(output.getvalue()))
    for ref in writer.pages[0]["/Resources"]["/Font"].values():
        font = ref.get_object()
        if "/FontDescriptor" not in font:
            continue
        descriptor = font["/FontDescriptor"]
        font[NameObject("/BaseFont")] = NameObject(alias)
        descriptor[NameObject("/FontName")] = NameObject(alias)
        descriptor[NameObject("/Flags")] = NumberObject(32)
        descriptor[NameObject("/StemV")] = NumberObject(stem)
        if explicit_weight is not None:
            descriptor[NameObject("/FontWeight")] = NumberObject(explicit_weight)
        if not embedded:
            del descriptor["/FontFile2"]
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize("snapshot", [False, True])
def test_original_embedded_weights_override_out_of_range_estimates(snapshot: bool) -> None:
    """真实六页源的普通/斜体字重为400，粗体为700，字符与快照路径保持一致。"""
    expected = {"CIDFont+F1": 700, "CIDFont+F2": 400, "CIDFont+F3": 400}
    with PDFDocument(SOURCE.read_bytes()) as document:
        assert document.page_count == 6
        counts = Counter()
        for index in range(document.page_count):
            page = document[index]
            chars = page.get_snapshot().text_geometry.chars if snapshot else page.get_chars()
            for char in chars:
                if char["char"].strip():
                    font = char["font"]
                    assert font["weight"] == expected[font["name"]]
                    counts[font["name"]] += len(char["char"])
        assert counts == {"CIDFont+F1": 102, "CIDFont+F2": 12, "CIDFont+F3": 17734}


def test_original_regular_body_has_no_bold_styles() -> None:
    """公开解析保留完整首页标题，六页正文不再携带误加粗样式。"""
    result = parse(SOURCE, parse_mode="txt")
    pages = result.to_dict()["pages"]
    title = pages[0]["blocks"][0]
    assert title["type"] == "doc_title"
    assert "".join(span["content"] for span in _spans(title)).endswith("Fuel Reprocessing Plants")
    paragraphs = [block for page in pages for block in page["blocks"] if block["type"] == "text"]
    assert any("Every applicant for a construction permit" in span["content"] for span in _spans(paragraphs))
    assert not any("bold" in span.get("styles", []) for span in _spans(paragraphs))
    # 第六页曾把普通正文误提为标题；字重恢复后审计段应连续，且不归到上一节。
    audit = [
        block for block in pages[5]["blocks"] if any("A comprehensive system" in span["content"] for span in _spans(block))
    ]
    assert len(audit) == 1 and audit[0]["type"] == "text"
    assert "".join(span["content"] for span in _spans(audit[0])).startswith("XVIII. Audits ")


@pytest.mark.parametrize("stem,alias", [(943, "/Opaque"), (500, "/ABCDEF+OtherFace"), (1000, "/Unrelated")])
def test_opaque_fonts_keep_real_inline_bold(stem: int, alias: str) -> None:
    """不同异常量级和资源名均恢复真实字重，同名字体不能互相污染缓存。"""
    payload = _opaque_font_pdf(stem, alias)
    for snapshot in (False, True):
        with PDFDocument(payload) as document:
            page = document[0]
            chars = page.get_snapshot().text_geometry.chars if snapshot else page.get_chars()
            ordinary = next(char for char in chars if char["char"] == "O")
            emphasized = next(char for char in chars if char["char"] == "e")
            assert ordinary["font"]["weight"] == 400
            assert emphasized["font"]["weight"] == 700
    spans = list(_spans(parse(payload, file_suffix="pdf", parse_mode="txt").to_dict()["pages"]))
    assert "emphasized words" in "".join(span["content"] for span in spans if "bold" in span.get("styles", []))
    assert "Ordinary words" in "".join(span["content"] for span in spans if "bold" not in span.get("styles", []))


@pytest.mark.parametrize("weight", [400, 700, 1000])
def test_valid_explicit_weights_are_preserved(weight: int) -> None:
    """合法显式字重不被嵌入字库覆盖，也不会读取或替换正常元数据。"""
    payload = _opaque_font_pdf(943, "/Opaque", explicit_weight=weight)
    with PDFDocument(payload) as document:
        assert {char["font"]["weight"] for char in document[0].get_chars() if char["char"].strip()} == {weight}


def test_unembedded_font_does_not_use_substitution_weight() -> None:
    """异常字重缺少嵌入证据时保持未知，不把平台替代字库当成源文档字重。"""
    payload = _opaque_font_pdf(943, "/Opaque", embedded=False)
    with PDFDocument(payload) as document:
        assert {char["font"]["weight"] for char in document[0].get_chars() if char["char"].strip()} == {-1}


def test_original_source_contains_regular_font_despite_large_stemv() -> None:
    """锁定源事实：正文没有显式字重，StemV为943，嵌入字体声明Regular/400。"""
    reader = PdfReader(SOURCE)
    font = reader.pages[0]["/Resources"]["/Font"]["/F3"]["/DescendantFonts"][0].get_object()
    descriptor = font["/FontDescriptor"]
    assert "/FontWeight" not in descriptor and descriptor["/StemV"] == 943
    with FontToolsFont(BytesIO(descriptor["/FontFile2"].get_data())) as embedded:
        assert embedded["OS/2"].usWeightClass == 400
        assert embedded["head"].macStyle & 1 == 0


@pytest.mark.parametrize("snapshot", [False, True])
@pytest.mark.parametrize("explicit_weight,fail_data,expected_reads", [(None, False, 4), (None, True, 2), (400, False, 0)])
def test_font_data_reads_are_bounded_and_failure_keeps_unknown(
    monkeypatch: pytest.MonkeyPatch, snapshot: bool, explicit_weight: int | None, fail_data: bool, expected_reads: int
) -> None:
    """每个异常字体只读取一次大小和数据；合法字重不读字库，失败也不传播异常估算。"""
    payload = _opaque_font_pdf(943, "/Opaque", explicit_weight=explicit_weight)
    original = raw.FPDFFont_GetFontData
    calls = []
    factory = ctypes.WINFUNCTYPE if hasattr(ctypes, "WINFUNCTYPE") else ctypes.CFUNCTYPE

    def read_data(font: Any, buffer: Any, capacity: int, length: Any) -> int:
        """保留真实 ABI 与句柄，仅记录读取次数或模拟 PDFium 无法提供字体数据。"""
        calls.append(capacity)
        return 0 if fail_data else original(font, buffer, capacity, length)

    monkeypatch.setattr(raw, "FPDFFont_GetFontData", factory(original.restype, *original.argtypes)(read_data))
    with PDFDocument(payload) as document:
        page = document[0]
        chars = page.get_snapshot().text_geometry.chars if snapshot else page.get_chars()
        weights = {char["font"]["weight"] for char in chars if char["char"].strip()}
    assert len(calls) == expected_reads
    assert weights == ({explicit_weight} if explicit_weight is not None else {-1} if fail_data else {400, 700})
