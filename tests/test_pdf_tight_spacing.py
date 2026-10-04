"""Local rules for non-CJK ink word boundaries, output materialization, and true PDF regression."""

from copy import deepcopy
import math
from pathlib import Path

import pytest

from docvortex.document.pdf.text._contracts import Bbox
from docvortex.document.pdf.text.spacing import join_tight_text, needs_tight_space
from docvortex.analyzers.native.pdf.inline.spacing import prepare_spacing_lines, apply_spacing_lines
from docvortex.analyzers.native.pdf.models import _LineItem


def _char(text, index, x, *, size=10.0, y=0.0):
    """Construct test characters in which the loose boxes overlap each other and the tight box has true word spacing."""
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
    """Two characters can be determined independently. The threshold is strictly greater than and only one space is inserted."""
    chars = [_char("A", 0, 0), _char("B", 1, 4 + gap)]
    original = deepcopy(chars)
    assert needs_tight_space(*chars) is expected
    assert join_tight_text(chars) == ("A B" if expected else "AB")
    assert chars[0]["tight_bbox"] == original[0]["tight_bbox"]
    assert chars[0]["bbox"].bbox == original[0]["bbox"].bbox


@pytest.mark.parametrize("pair", ["中文", "中A", "A文", "あA", "A한", "𠀀A", "A ", " A", "A,", "(A", "A+", "A²"])
def test_excluded_boundaries(pair):
    """CJK, punctuation, symbols and whitespace will not trigger new rules even if there are large spaces."""
    assert not needs_tight_space(_char(pair[0], 0, 0), _char(pair[1], 1, 30))


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_direction_and_script_guards(angle):
    """The four orthogonal directions retain the same word spacing, and misaligned lines, superscripts, subscripts, and discontinuous members are rejected."""
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
    """Missing, non-limited, skewed, or cross-baseline evidence cannot trigger new spaces."""
    chars = [_char("A", 0, 0), _char("B", 1, 8)]
    chars[1][field] = value
    assert not needs_tight_space(*chars)


def test_mixed_line_and_final_block_ownership():
    """The English in the mixed ranking can be repaired, the code and formulas are not rewritten, the materialization is idempotent and the structure is not changed."""
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
    """loose box overlap of real text still fills in word boundaries, and no spurious spaces are added to the source character sequence."""
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
    """Cell materialization reuses word boundaries and does not change source members; superscripts and subscripts and adjacent cells do not participate in filling spaces."""
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
    """The character projection after the original table recovery fails can also fill in word boundaries, and existing blanks will not be repeated."""
    from docvortex.analyzers.native.pdf.spatial_text import project_pdf_table_text, project_pdf_spatial_text

    chars = [_char("A", 0, 0), _char("B", 1, 8)]
    assert project_pdf_table_text(chars, (0, 0, 30, 20)).strip() == "A B"
    assert project_pdf_spatial_text(chars, (0, 0, 30, 20)).strip() == "AB"


def test_transformed_unit_font_size_is_not_a_page_em():
    """The text matrix's enlarged 1pt original font size does not allow the normal 10pt glyph to be broken up word for word."""
    chars = [_char("A", 0, 0, size=1), _char("B", 1, 5, size=1)]
    assert not needs_tight_space(*chars)
    assert join_tight_text(chars) == "AB"


@pytest.mark.parametrize("name,flags", [("CourierNew", 0), ("SourceHanMono-Normal", 0), ("Fixture", 1)])
def test_monospace_ink_gap_is_not_a_word_boundary(name, flags):
    """Narrow letter spacing in a monospaced font cannot break up VirtualBox or the identifier."""
    chars = [_char("i", 0, 0), _char("r", 1, 8)]
    for char in chars:
        char["font"].update(name=name, flags=flags)
    assert not needs_tight_space(*chars)


def test_narrow_digits_remain_one_number():
    """Consecutive narrow numbers 11 in financial tables are not broken apart by blank ink boxes."""
    assert not needs_tight_space(_char("1", 0, 0), _char("1", 1, 8))


@pytest.mark.parametrize("name", ["中文论文2.pdf", "demo1.pdf", "caibao1.pdf"])
def test_owned_boundary_indices_match_python(name):
    """Rust batch word boundaries are consistent with Python pairwise rules, covering real targets and font size counterexamples."""
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
    """Existing cross-line hyphenation does not invalidate the reliable word boundary projection of the entire line."""
    from docvortex.analyzers.native.pdf.inline.spacing import PDFTextSpacingLine

    lines = [PDFTextSpacingLine((0, 0, 90, 10), "deeplan-", 0, (4,)), PDFTextSpacingLine((0, 10, 90, 20), "guage", 1, ())]
    blocks = [{"type": "text", "bbox": (0, 0, 90, 20), "content": "deeplanguage"}]
    apply_spacing_lines(blocks, lines, (100, 100))
    assert blocks[0]["content"] == "deep language"


def test_spacing_is_preserved_inside_link_materialization():
    """The new spaces are materialized before the link interval, and the display text and link target remain intact."""
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
    """Full document verification Flash Titles, hyphenated references, and native cells simultaneously restore word spacing."""
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
