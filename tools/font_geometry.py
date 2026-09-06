"""记录实际 PDF 字体字节和未规范化的字符几何，供独立进程及三平台差分使用。"""

from __future__ import annotations

import argparse
from contextlib import closing, nullcontext
import ctypes
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pypdfium2.raw as raw

from docvortex.document.pdf.pdfium import pdfium_guard


def capture_geometry(source: Path, *, system_fonts: bool = False) -> dict[str, Any]:
    """在关闭句柄前物化全部字符；系统模式仅用于独立进程保存替换前参考值。"""
    fonts: dict[str, Any] = {}
    pages = []
    with nullcontext() if system_fonts else pdfium_guard():
        with pdfium.PdfDocument(source) as document:
            for page_index in range(len(document)):
                with closing(document[page_index]) as page, closing(page.get_textpage()) as textpage:
                    font_ids: dict[int, str] = {}
                    chars = []
                    for index in range(textpage.count_chars()):
                        obj = raw.FPDFText_GetTextObject(textpage.raw, index)
                        font = raw.FPDFTextObj_GetFont(obj) if obj else None
                        address = ctypes.cast(font, ctypes.c_void_p).value or 0
                        if address not in font_ids:
                            font_id = f"{page_index}:{len(font_ids)}"
                            font_ids[address] = font_id
                            name = ctypes.create_string_buffer(512)
                            data_size = ctypes.c_size_t()
                            data_hash = None
                            embedded = None
                            if font:
                                raw.FPDFFont_GetBaseFontName(font, name, len(name))
                                embedded = raw.FPDFFont_GetIsEmbedded(font)
                                if raw.FPDFFont_GetFontData(font, None, 0, data_size) and data_size.value:
                                    buffer = (ctypes.c_ubyte * data_size.value)()
                                    if raw.FPDFFont_GetFontData(font, buffer, len(buffer), data_size):
                                        data_hash = sha256(bytes(buffer)).hexdigest()
                            fonts[font_id] = {
                                "name": name.value.decode("utf-8", errors="replace"),
                                "name_hex": name.value.hex(),
                                "embedded": embedded,
                                "data_sha256": data_hash,
                            }
                        tight = [ctypes.c_double() for _ in range(4)]
                        loose = raw.FS_RECTF()
                        origin = [ctypes.c_double(), ctypes.c_double()]
                        # 坐标按 PDF 原始坐标保存，不经过页面旋转或 Flash 几何修复。
                        chars.append(
                            {
                                "index": index,
                                "unicode": raw.FPDFText_GetUnicode(textpage.raw, index),
                                "generated": raw.FPDFText_IsGenerated(textpage.raw, index) == 1,
                                "font": font_ids[address],
                                "tight": [value.value for value in tight]
                                if raw.FPDFText_GetCharBox(textpage.raw, index, *tight)
                                else None,
                                "loose": [loose.left, loose.right, loose.bottom, loose.top]
                                if raw.FPDFText_GetLooseCharBox(textpage.raw, index, loose)
                                else None,
                                "origin": [value.value for value in origin]
                                if raw.FPDFText_GetCharOrigin(textpage.raw, index, *origin)
                                else None,
                            }
                        )
                    pages.append({"size": list(page.get_size()), "rotation": page.get_rotation(), "chars": chars})
    return {"source_sha256": sha256(source.read_bytes()).hexdigest(), "fonts": fonts, "pages": pages}


def main() -> None:
    """在全新解释器中按指定策略记录原始值，不在已使用的 PDFium 上切换提供器。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--system-fonts", action="store_true")
    args = parser.parse_args()
    args.output.write_text(
        json.dumps(capture_geometry(args.source, system_fonts=args.system_fonts), ensure_ascii=False), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
