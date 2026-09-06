"""以原生字体数据和字符几何验证替代边界，不依赖 PNG 编码的一致性。"""

from __future__ import annotations

from collections import Counter
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from docgale.document.pdf.font_runtime import _FONT_SHA256
from tools.font_geometry import capture_geometry

_ROOT = Path(__file__).resolve().parents[1]


def _system_geometry(source: Path, destination: Path) -> dict[str, Any]:
    """从独立解释器读取默认 PDFium，保证从未安装过 DocGale 的字体接口。"""
    completed = subprocess.run(
        [sys.executable, str(_ROOT / "tools/font_geometry.py"), str(source), str(destination), "--system-fonts"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(destination.read_text(encoding="utf-8"))


def _assert_replacement_boundary(old: dict[str, Any], new: dict[str, Any]) -> tuple[int, int]:
    """源字符不变；缺字环境可改变 PDFium 自动补空格数量，但索引仍来自原始 textpage。"""
    assert old["source_sha256"] == new["source_sha256"]
    assert len(old["pages"]) == len(new["pages"])
    replaced_names = {font["name_hex"] for font in new["fonts"].values() if font["data_sha256"] == _FONT_SHA256}
    replaced = embedded = 0
    for before_page, after_page in zip(old["pages"], new["pages"]):
        assert before_page["size"] == after_page["size"]
        assert before_page["rotation"] == after_page["rotation"]
        assert [char["index"] for char in after_page["chars"]] == list(range(len(after_page["chars"])))
        protected = []
        cjk = []
        for page, data in ((before_page, old), (after_page, new)):
            stable = []
            replaced_codes = []
            for char in page["chars"]:
                font = data["fonts"][char["font"]]
                if char["generated"] or font["embedded"] is None:
                    continue
                if font["embedded"] == 0 and font["name_hex"] in replaced_names:
                    if not chr(char["unicode"]).isspace():
                        replaced_codes.append(char["unicode"])
                    if data is new:
                        assert font["data_sha256"] == _FONT_SHA256
                        replaced += 1
                else:
                    stable.append((char, font))
            protected.append(stable)
            cjk.append(replaced_codes)
        # 无可用系统 CJK 字库时，旧 PDFium 会省略缺失字形；新结果不得丢失旧有字符。
        # 三平台的新策略字符数量、Unicode 和索引仍由平台差分严格逐项检查。
        assert Counter(cjk[0]) <= Counter(cjk[1])
        if len(cjk[0]) == len(cjk[1]):
            assert cjk[0] == cjk[1]
        assert len(protected[0]) == len(protected[1])
        for (before, source_font), (after, target_font) in zip(*protected):
            assert before["unicode"] == after["unicode"]
            assert target_font == source_font
            assert all(after[field] == before[field] for field in ("loose", "tight", "origin"))
            embedded += source_font["embedded"] == 1
    return replaced, embedded


@pytest.mark.parametrize("name", ["中文论文3.pdf", "中文论文4.pdf"])
def test_real_papers_preserve_unicode_and_embedded_fonts(name: str, tmp_path: Path) -> None:
    """逐页确认两篇论文的字体接管真实生效，同时保护原始文本及嵌入字体。"""
    source = _ROOT / "demo/pdfs" / name
    old = _system_geometry(source, tmp_path / "system.json")
    new = capture_geometry(source)
    replaced, embedded = _assert_replacement_boundary(old, new)
    assert replaced > 100 and embedded > 100


def _mixed_font_pdf(destination: Path) -> None:
    """生成嵌入 CJK、未嵌入 CJK、英文/符号及旋转横排文本，使用已有运行时依赖。"""
    from importlib import resources
    from pypdf import PdfReader, PdfWriter

    payload = BytesIO()
    font_resource = resources.files("docgale").joinpath("resources/fonts/DroidSansFallbackFull.ttf")
    pdfmetrics.registerFont(TTFont("EmbeddedDroidFixture", BytesIO(font_resource.read_bytes())))
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    canvas = Canvas(payload, pagesize=(595.3, 841.7), pageCompression=0)
    for angle in (0, 90, 180, 270):
        canvas.saveState()
        canvas.translate(300, 400)
        canvas.rotate(angle)
        canvas.setFont("STSong-Light", 11)
        canvas.drawString(-120, 20, "中文繁體、，。（）龘㐀 Tokyo 日本語 한글 123")
        canvas.setFont("EmbeddedDroidFixture", 11)
        canvas.drawString(-120, 0, "嵌入中文保持原字形：微风，龘。")
        canvas.setFont("Helvetica-BoldOblique", 11)
        canvas.drawString(-120, -20, "Latin unchanged ABC 0123")
        canvas.setFont("Symbol", 11)
        canvas.drawString(-120, -40, "αβγΣ∑")
        canvas.restoreState()
        canvas.showPage()
    canvas.save()
    writer = PdfWriter()
    for index, page in enumerate(PdfReader(payload).pages):
        page.rotate(index * 90)
        writer.add_page(page)
    with destination.open("wb") as stream:
        writer.write(stream)


def test_rotated_mixed_font_pdf_preserves_replacement_boundary(tmp_path: Path) -> None:
    """旋转页面及横排文本仍只替换未嵌入 CJK，不修改字体嵌入或非 CJK 抽取。"""
    source = tmp_path / "mixed.pdf"
    _mixed_font_pdf(source)
    old = _system_geometry(source, tmp_path / "system.json")
    new = capture_geometry(source)
    replaced, embedded = _assert_replacement_boundary(old, new)
    assert replaced > 50 and embedded > 50
    assert {page["rotation"] for page in new["pages"]} == {0, 90, 180, 270}
