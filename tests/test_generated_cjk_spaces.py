"""生成中文空格的证据、几何保护及内容物化回归。"""

from copy import deepcopy
from io import BytesIO
from pathlib import Path
import math

import pytest
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas

from docvortex.document.pdf import PDFDocument
from docvortex.document.pdf.text.spacing import is_generated_cjk_space, join_tight_text, _local_box
from docvortex.analyzers.native.pdf.inline.spacing import apply_spacing_lines, prepare_spacing_lines
from docvortex.analyzers.native.pdf.models import _LineItem


def _chars(text: str, *, generated: bool | None = True, angle: int = 0) -> list[dict]:
    """建立连续源编号和可旋转的同基线字符，不省略源空格。"""
    chars = []
    for index, value in enumerate(text):
        box = _local_box((index * 6, 0, index * 6 + 10, 10), (-angle) % 360)
        origin = _local_box((index * 6, 10, index * 6, 10), (-angle) % 360)[:2]
        char = {
            "char": value,
            "char_idx": index,
            "bbox": box,
            "tight_bbox": box,
            "origin": origin,
            "writing_angle": math.radians(angle),
            "font": {"name": "Fixture", "size": 1.0, "flags": 0},
        }
        if generated is not None:
            char["is_generated"] = generated if value == " " else False
        chars.append(char)
    return chars


@pytest.mark.parametrize(
    "text,expected",
    [
        ("卫 星", "卫星"),
        ("候 、", "候、"),
        ("、 全", "、全"),
        ("卫 InSAR 技", "卫 InSAR 技"),
        ("A B", "A B"),
        ("𠀀 文", "𠀀文"),
        ("、 ，", "、 ，"),
    ],
)
@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_generated_spaces_only_at_chinese_boundaries(text: str, expected: str, angle: int) -> None:
    """源字号为 1 或旋转不影响可靠基线判断，中英词界和标点间空格保留。"""
    chars = _chars(text, angle=angle)
    original = deepcopy(chars)
    assert join_tight_text(chars) == expected
    assert chars == original


@pytest.mark.parametrize("generated", [False, None])
def test_real_or_unconfirmed_spaces_survive(generated: bool | None) -> None:
    """真实空格和未知生成证据一律保留。"""
    assert join_tight_text(_chars("中 文", generated=generated)) == "中 文"


@pytest.mark.parametrize(
    "field,value",
    [
        ("origin", (24, 30)),
        ("writing_angle", 0.3),
        ("char_idx", 10),
        ("tight_bbox", (12, 20, 22, 30)),
        ("tight_bbox", (12, 0, 22, float("nan"))),
    ],
)
def test_different_lines_and_incomplete_geometry_survive(field: str, value: object) -> None:
    """跨基线、不同方向、源编号不连续或非法框不能删空格。"""
    chars = _chars("中 文")
    chars[-1][field] = value
    assert not is_generated_cjk_space(*chars)


def test_flash_materialization_protects_code_and_equations() -> None:
    """仅修改最终自然语言文字，代码、公式和源行均保留空格。"""
    chars = _chars("中 文")
    line = _LineItem(text="中 文", bbox=(0, 0, 36, 10), angle=0, source_index=0, chars=chars)
    evidence = prepare_spacing_lines([line], set())
    for kind in ("text", "code", "equation"):
        blocks = [{"type": kind, "bbox": (0, 0, 36, 10), "content": "中 文"}]
        apply_spacing_lines(blocks, evidence, (100, 100))
        apply_spacing_lines(blocks, evidence, (100, 100))
        assert blocks[0]["content"] == ("中文" if kind == "text" else "中 文")
    assert line.text == "中 文" and len(chars) == 3


def spaced_pdf() -> bytes:
    """用中文文字矩阵字距产生 PDFium 空格，同时放入真实中英文空格作反例。"""
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    stream = BytesIO()
    canvas = Canvas(stream)
    canvas.setFont("STSong-Light", 12)
    for index, char in enumerate("卫星技术全天候"):
        canvas.drawString(30 + index * 15, 700, char)
    canvas.drawString(30, 650, "中 文 InSAR 技术")
    canvas.save()
    return stream.getvalue()


def test_real_pdf_keeps_generated_character_records() -> None:
    """真实 PDF 的生成空格具有可选证据，物化后仍保留全部索引及几何记录。"""
    with PDFDocument(spaced_pdf()) as document:
        geometry = document.get_page_chars_with_geometry(0)
    chars = geometry.chars
    raw = "".join(char["char"] for char in chars)
    assert "卫 星 技 术 全 天 候" in raw
    assert "卫星技术全天候" in join_tight_text(chars)
    assert "中 文 InSAR 技术" in join_tight_text(chars)
    assert all("is_generated" in char for char in chars)
    assert "".join(char["char"] for char in chars) == raw


@pytest.mark.parametrize("gap,expected", [(4.99, True), (5.0, True), (5.01, False), (20, False)])
def test_wide_generated_field_separator_survives(gap: float, expected: bool) -> None:
    """字段和姓名之间的大生成空白保留；半字形高度阈值按严格大于拒绝。"""
    chars = _chars("中 文")
    chars[2]["tight_bbox"] = (10 + gap, 0, 20 + gap, 10)
    assert is_generated_cjk_space(*chars) is expected


@pytest.mark.parametrize("page,expected", [(2, "序号 包号 包名称 包预算（元） 包最高限价"), (5, "杨永丽 田兴 刘颖颖 李帆帆")])
def test_real_generated_field_separators_survive(page: int, expected: str) -> None:
    """招标原件中已确认生成的字段和姓名间隔保留，防止普通中文过滤误吞分隔。"""
    path = Path(__file__).parents[1] / "demo/pdfs/demo6.pdf"
    with PDFDocument(path.read_bytes()) as document:
        chars = document[page].get_chars_with_geometry().chars
    assert expected in join_tight_text(chars)


def test_generated_space_join_accepts_iterators() -> None:
    """公开拼接函数仍接受生成器输入，不能因新增邻居查找要求调用方改用列表。"""
    assert join_tight_text(iter(_chars("中 文"))) == "中文"
