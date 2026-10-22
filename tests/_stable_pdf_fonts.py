"""为逐字段金标固定未嵌入字体，避免操作系统替代字库影响测试输入。"""

from io import BytesIO
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import NameObject


def stable_font_pdf(source: bytes | Path) -> bytes:
    """保留嵌入字库与编码，仅将缺失字库的族名改为运行时自带的固定字体。"""
    from docvortex.document.pdf import initialize_pdfium_runtime

    initialize_pdfium_runtime()
    data = source.read_bytes() if isinstance(source, Path) else source
    writer = PdfWriter(clone_from=BytesIO(data))
    for obj in writer._objects:
        if not isinstance(obj, dict) or obj.get("/Type") != "/Font" or "/BaseFont" not in obj:
            continue
        descendants = obj.get("/DescendantFonts")
        font = descendants[0].get_object() if descendants else obj
        descriptor = font.get("/FontDescriptor")
        descriptor = descriptor.get_object() if descriptor else {}
        if any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3")):
            continue
        # 固定字体提供器识别此名称；不改变原文编码、字符宽度和已有嵌入字体。
        obj[NameObject("/BaseFont")] = NameObject("/DroidSansFallbackFull")
        font[NameObject("/BaseFont")] = NameObject("/DroidSansFallbackFull")
        if descriptor:
            descriptor[NameObject("/FontName")] = NameObject("/DroidSansFallbackFull")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()
