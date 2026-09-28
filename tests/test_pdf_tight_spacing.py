"""非 CJK 墨迹词界的局部规则、输出物化及真实 PDF 回归。"""

from copy import deepcopy
import math
from pathlib import Path

import pytest

from docvortex.document.pdf.text._contracts import Bbox
from docvortex.document.pdf.text.spacing import join_tight_text, needs_tight_space
from docvortex.analyzers.native.pdf.inline.spacing import prepare_spacing_lines, apply_spacing_lines
from docvortex.analyzers.native.pdf.models import _LineItem


def _char(text, index, x, *, size=10.0, y=0.0):
    """构造 loose 框互相覆盖而 tight 框具有真实词距的测试字符。"""
    return {
        "char": text,
        "char_idx": index,
        "bbox": Bbox([x, y, x + 10, y + 10]),
        "tight_bbox": (x, y + 1, x + 4, y + 9),
        "origin": (x, y + 10),
        "rotation": 0.0,
        "writing_angle": 0.0,
        "font": {"name": "Fixture", "size": size, "weight": 400, "flags": 0},
    }


@pytest.mark.parametrize("gap, expected", [(0, False), (2.49, False), (2.5, False), (2.51, True), (100, True)])
def test_pair_threshold_without_sample_count(gap, expected):
    """两个字符即可独立判定，阈值采用严格大于并且只插一个空格。"""
    chars = [_char("A", 0, 0), _char("B", 1, 4 + gap)]
    original = deepcopy(chars)
    assert needs_tight_space(*chars) is expected
    assert join_tight_text(chars) == ("A B" if expected else "AB")
    assert chars[0]["tight_bbox"] == original[0]["tight_bbox"]
    assert chars[0]["bbox"].bbox == original[0]["bbox"].bbox


@pytest.mark.parametrize("pair", ["中文", "中A", "A文", "あA", "A한", "𠀀A", "A ", " A", "A,", "(A", "A+", "A²"])
def test_excluded_boundaries(pair):
    """CJK、标点、符号及空白即使有大留白也不触发新增规则。"""
    assert not needs_tight_space(_char(pair[0], 0, 0), _char(pair[1], 1, 30))


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_direction_and_script_guards(angle):
    """四个正交方向保留相同词距，错行、上下标和不连续成员被拒绝。"""
    from docvortex.document.pdf.text.spacing import _local_box

    chars = [_char("A", 0, 0), _char("B", 1, 8)]
    for char in chars:
        char["tight_bbox"] = _local_box(char["tight_bbox"], (-angle) % 360)
        x, y = char["origin"]
        transformed = _local_box((x, y, x, y), (-angle) % 360)
        char["origin"] = transformed[:2]
        char["writing_angle"] = math.radians(angle)
    assert needs_tight_space(*chars)
    chars[1]["font"]["name"] = "DifferentFont"
    assert needs_tight_space(*chars)
    chars[1]["char_idx"] = 3
    assert not needs_tight_space(*chars)
    chars[1]["char_idx"] = 1
    chars[1]["font"]["size"] = 6
    assert not needs_tight_space(*chars)


@pytest.mark.parametrize(
    "field, value",
    [("tight_bbox", None), ("tight_bbox", (0, 0, float("nan"), 10)), ("writing_angle", 0.3), ("origin", (8, 30))],
)
def test_unreliable_geometry_is_unchanged(field, value):
    """缺失、非有限、倾斜或跨基线证据不能触发新增空格。"""
    chars = [_char("A", 0, 0), _char("B", 1, 8)]
    chars[1][field] = value
    assert not needs_tight_space(*chars)


def test_mixed_line_and_final_block_ownership():
    """混排行内英文可修复，代码和公式不改写，物化幂等且不改变结构。"""
    chars = [_char(text, i, x) for i, (text, x) in enumerate(zip("中AIML文", [0, 12, 17, 25, 30, 45]))]
    line = _LineItem(text="中AIML文", bbox=(0, 0, 55, 10), angle=0, source_index=0, chars=chars)
    evidence = prepare_spacing_lines([line])
    for kind in ("text", "code", "equation"):
        blocks = [{"type": kind, "bbox": (0, 0, 55, 10), "content": "中AIML文"}]
        apply_spacing_lines(blocks, evidence, (100, 100))
        apply_spacing_lines(blocks, evidence, (100, 100))
        assert blocks[0]["content"] == ("中AI ML文" if kind == "text" else "中AIML文")
        assert blocks[0]["bbox"] == (0, 0, 55, 10)
    assert line.text == "中AIML文"


def test_real_paper_words_and_cjk_source_preserved():
    """真实正文的 loose 框重叠仍能补词界，源字符序列不添加伪造空格。"""
    from docvortex.document.pdf import PDFDocument

    path = Path(__file__).resolve().parents[1] / "demo/pdfs/中文论文2.pdf"
    with PDFDocument(path.read_bytes()) as document:
        geometry = document.get_page_chars_with_geometry(0)
    chars = geometry.chars
    raw = "".join(char["char"] for char in chars)
    restored = join_tight_text(chars)
    assert "LargeLanguageModels" in raw
    assert "Large Language Models" in restored
    assert "Natural Language Processing" in restored
    assert "School of Computer Science" in restored
    assert "100871" in restored and "2023-07-19" in restored
    assert "".join(char["char"] for char in chars) == raw


def test_table_cell_spacing_and_script_protection():
    """单元格物化复用词界且不改源成员；上下标和相邻单元格不参与补空格。"""
    from docvortex.analyzers.native.pdf._table_recovery.contracts import NativeTableGlyph, NativeTableCell
    from docvortex.analyzers.native.pdf.table_text_styles import _render_styled_cell

    chars = [_char("A", 0, 0), _char("B", 1, 8)]
    glyphs = [NativeTableGlyph(i, i, c["char"], tuple(c["bbox"].bbox), 0) for i, c in enumerate(chars)]
    cell = NativeTableCell(0, 0, 1, 1, (0, 0, 25, 12), "AB", (0, 1))
    mapping = dict(enumerate(chars))
    assert _render_styled_cell(cell, glyphs, 10, {}, chars_by_source=mapping) == "A B"
    assert _render_styled_cell(cell, glyphs, 10, {1: "sup"}, chars_by_source=mapping) == "A<sup>B</sup>"
    left = NativeTableCell(0, 0, 1, 1, (0, 0, 8, 12), "A", (0,))
    right = NativeTableCell(0, 1, 1, 1, (8, 0, 25, 12), "B", (1,))
    assert _render_styled_cell(left, glyphs[:1], 10, {}, chars_by_source=mapping) == "A"
    assert _render_styled_cell(right, glyphs[1:], 10, {}, chars_by_source=mapping) == "B"
    assert cell.content == "AB" and cell.source_char_indices == (0, 1)


def test_table_projection_fallback_spacing():
    """原生表格恢复失败后的字符投影也能补词界，已有空白不会重复。"""
    from docvortex.analyzers.native.pdf.spatial_text import project_pdf_table_text, project_pdf_spatial_text

    chars = [_char("A", 0, 0), _char("B", 1, 8)]
    assert project_pdf_table_text(chars, (0, 0, 30, 20)).strip() == "A B"
    assert project_pdf_spatial_text(chars, (0, 0, 30, 20)).strip() == "AB"


def test_transformed_unit_font_size_is_not_a_page_em():
    """文字矩阵放大的 1pt 原始字号不能让正常 10pt 字形被逐字拆开。"""
    chars = [_char("A", 0, 0, size=1), _char("B", 1, 5, size=1)]
    assert not needs_tight_space(*chars)
    assert join_tight_text(chars) == "AB"


@pytest.mark.parametrize("name,flags", [("CourierNew", 0), ("SourceHanMono-Normal", 0), ("Fixture", 1)])
def test_monospace_ink_gap_is_not_a_word_boundary(name, flags):
    """等宽字体中的窄字母留白不能把 VirtualBox 或标识符拆开。"""
    chars = [_char("i", 0, 0), _char("r", 1, 8)]
    for char in chars:
        char["font"].update(name=name, flags=flags)
    assert not needs_tight_space(*chars)


def test_narrow_digits_remain_one_number():
    """财务表格中的连续窄数字 11 不因墨迹框留白被拆开。"""
    assert not needs_tight_space(_char("1", 0, 0), _char("1", 1, 8))


@pytest.mark.parametrize("name", ["中文论文2.pdf", "demo1.pdf", "caibao1.pdf"])
def test_owned_boundary_indices_match_python(name):
    """Rust 批量词界与 Python 逐对规则一致，覆盖真实目标和字号尺度反例。"""
    from docvortex.document.pdf import PDFDocument
    from docvortex._compute_backend import get_native

    if get_native() is None:
        pytest.skip("requires Rust snapshot")
    path = Path(__file__).resolve().parents[1] / "demo/pdfs" / name
    with PDFDocument(path.read_bytes()) as document:
        owner = document[0].get_text_snapshot()
    chars = owner.materialize_geometry().chars
    expected = [b["char_idx"] for a, b in zip(chars, chars[1:]) if needs_tight_space(a, b)]
    assert owner.tight_space_indices() == expected


def test_spacing_survives_existing_line_end_dehyphenation():
    """已有跨行去连字符不能让整行的可靠词界投影失效。"""
    from docvortex.analyzers.native.pdf.inline.spacing import PDFTextSpacingLine

    lines = [PDFTextSpacingLine((0, 0, 90, 10), "deeplan-", 0, (4,)), PDFTextSpacingLine((0, 10, 90, 20), "guage", 1, ())]
    blocks = [{"type": "text", "bbox": (0, 0, 90, 20), "content": "deeplanguage"}]
    apply_spacing_lines(blocks, lines, (100, 100))
    assert blocks[0]["content"] == "deep language"


def test_spacing_is_preserved_inside_link_materialization():
    """新增空格先于链接区间物化，显示文字和链接目标都保持完整。"""
    from docvortex.analyzers.native.pdf.inline.materialize import apply_pdf_inline_evidence
    from docvortex.analyzers.native.pdf.inline.spacing import PDFTextSpacingLine
    from docvortex.analyzers.native.pdf.inline.types import PDFTextLinkLine, PDFTextLinkRange

    bbox = (0, 0, 30, 10)
    blocks = [{"type": "text", "bbox": bbox, "content": "AIML"}]
    apply_spacing_lines(blocks, [PDFTextSpacingLine(bbox, "AIML", 0, (2,))], (100, 100))
    links = [PDFTextLinkLine(bbox, "AIML", (PDFTextLinkRange(0, 4, "https://example.test"),), 0)]
    apply_pdf_inline_evidence(blocks, links, [], [], (100, 100))
    link = blocks[0]["content"][0]
    assert link["type"] == "hyperlink" and link["url"] == "https://example.test"
    assert "".join(span["content"] for span in link["content"]) == "AI ML"


def test_real_flash_public_words_and_table():
    """完整文档验证 Flash 标题、断词参考文献和原生单元格同时恢复词距。"""
    from docvortex.analyzers.native import PdfModel
    from docvortex.document.pdf import PDFDocument
    import json

    path = Path(__file__).resolve().parents[1] / "demo/pdfs/中文论文2.pdf"
    with PDFDocument(path.read_bytes()) as document:
        pages = PdfModel().predict(document)
    content = json.dumps(pages, ensure_ascii=False)
    assert len(pages) == 23
    assert "Large Language Models" in content
    assert "Natural Language Processing" in content
    assert "Pre-training of deep bidirectional transformers" in content
    assert "Proceedings of the 58th Annual Meeting" in content
    assert any("Chatbot Arena" in block.get("content", "") for block in pages[1] if block["type"] == "table")
    assert "LargeLanguageModels" not in content
