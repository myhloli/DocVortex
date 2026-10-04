"""Use real screenshots and counterexamples to verify Flash frame, formula, paragraph and three-line table repair."""

from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
from io import BytesIO
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from reportlab.pdfgen.canvas import Canvas

from _flash_pdf_test_utils import _text_line
from docvortex.analyzers.native.pdf import auxiliary_text, formulas, graphics, pipeline
from docvortex.analyzers.native.pdf._table_recovery import NativeTableInput, NativeTableRule
from docvortex.document.pdf import PDFDocument

FIXTURES = Path(__file__).parent / "pdfs" / "flash_layout"


def _text(value: object) -> str:
    """Read visible text in strings and inline styles to avoid interpreting style differences as missing content."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text(item) for item in value)
    if isinstance(value, dict):
        return _text(value.get("content", ""))
    return ""


@lru_cache(maxsize=None)
def _pages(name: str) -> list[list[dict]]:
    """Cache the complete native analysis of the real screenshot, and the test does not rely on external disks."""
    with PDFDocument(str(FIXTURES / f"{name}.pdf")) as document:
        return pipeline._analyze_native_document(document)


def test_real_unnumbered_math_components_keep_prose_outside() -> None:
    """The three interline formulas must be fully claimed, and the subscripts must not become the main text or subtitles."""
    page = _pages("math_display_formulas")[0]
    equations = [block for block in page if block["type"] == "equation"]
    assert len(equations) == 3
    for equation, band in zip(equations, [(0.25, 0.315), (0.48, 0.54), (0.58, 0.61)], strict=True):
        assert band[0] <= equation["bbox"][1] < equation["bbox"][3] <= band[1]
        assert _text(equation["content"]) == ""
        assert not any(
            block["type"] in {"text", "paragraph_title"} and equation["bbox"][1] <= block["bbox"][1] < equation["bbox"][3]
            for block in page
        )
    assert any("其中" in _text(block["content"]) and block["type"] == "text" for block in page)


def test_math_note_merges_without_losing_paired_scripts() -> None:
    """The superscript and subscript of the combination number across run and the comments belong to the same text block, and the text m and k will not be raised or lowered."""
    page = _pages("math_display_formulas")[0]
    notes = [block for block in page if 0.31 < block["bbox"][1] < 0.34]
    assert len(notes) == 1 and notes[0]["type"] == "text"
    spans = notes[0]["content"]
    assert _text(spans) == "注: 当 m < k 时, 规定 Ckm = 0."
    assert [(span["content"], span.get("styles")) for span in spans if span.get("styles")] == [
        ("k", ["superscript"]),
        ("m", ["subscript"]),
    ]
    assert sum(block["type"] == "equation" for block in page) == 3


@pytest.mark.parametrize("barrier", ["explicit_split", "no_ink", "separate_columns", "multichar_script", "table"])
def test_paired_script_merge_respects_missing_evidence_and_barriers(barrier: str) -> None:
    """Pairwise corner complement evidence cannot bypass explicit boundaries, missing geometry, neighbor distances, or table claims."""
    from docvortex.analyzers.native.pdf import line_merging
    from docvortex.analyzers.native.pdf.models import _TextLane

    with PDFDocument(str(FIXTURES / "math_display_formulas.pdf")) as document:
        source = pipeline._collect_document_sources(document).page_sources[0]
    lines = [line for line in source.lines if 264 < line.bbox[1] < 280]
    assert len(lines) == 2
    if barrier == "explicit_split":
        lines[1].preserve_split_boundary = True
    elif barrier == "no_ink":
        for char in lines[1].chars:
            char.pop("tight_bbox", None)
    elif barrier == "separate_columns":
        for char in lines[1].chars:
            if char.get("tight_bbox") is not None:
                x0, y0, x1, y1 = char["tight_bbox"]
                char["tight_bbox"] = (x0 + 30, y0, x1 + 30, y1)
    elif barrier == "multichar_script":
        continuation = lines[1].chars[1]
        continuation.update(
            char="r",
            bbox=(236, 272, 240, 279),
            tight_bbox=(236, 274, 240, 278),
            origin=(236, 277.65),
            font=dict(lines[1].chars[0]["font"]),
        )
        lines[1].text = "mr = 0."
    members = [(line, line.bbox) for line in lines]
    lane = _TextLane(100, 300, members)
    tables = [(95, 260, 270, 285)] if barrier == "table" else []
    assert line_merging._classify_overlapping_inline_cluster(members, lane, 10.6, tables) is None


def test_real_form_labels_and_blank_mapped_bracket_are_inside() -> None:
    """Form Tightening cannot discard edge labels, and braces mapped to spaces must still be included in formula cropping."""
    pages = _pages("bloom_form_labels")
    images = [block for block in pages[0] if block["type"] == "image"]
    assert [_text(block["content"]).count("3 hashes") for block in images] == [1, 3]
    last_image = next(block for block in pages[2] if block["type"] == "image")
    assert last_image["bbox"][3] >= 500.0 / 792.0 - 0.001
    assert not any(block["type"] != "image" and 0.62 < block["bbox"][1] < 0.64 for block in pages[2])
    assert any("Figure 3:" in _text(block["content"]) and block["type"] != "image" for block in pages[2])
    equation = next(block for block in pages[1] if block["type"] == "equation")
    with PDFDocument(str(FIXTURES / "bloom_form_labels.pdf")) as document:
        chars = document._extract_native_page(1).text_geometry.chars
    left_bracket = next(
        char
        for char in chars
        if char.get("char") == " " and char.get("tight_bbox") and 253 < char["bbox"][0] < 254 and 227 < char["bbox"][1] < 229
    )
    assert equation["bbox"][0] <= left_bracket["tight_bbox"][0] / 612.0 + 0.001


def test_real_journal_title_rules_and_marginal_formulas() -> None:
    """Expose chunking while maintaining header merging, separator breaks, footnote attribution, and formula numbering."""
    pages = _pages("journal_layout_tables")
    titles = [block for block in pages[0] if block["type"] == "doc_title"]
    assert len(titles) == 1
    assert _text(titles[0]["content"]).endswith("perceptron neural network")
    copyright_block = next(block for block in pages[0] if _text(block["content"]).startswith("© 2024"))
    date_block = next(block for block in pages[0] if "Published online:" in _text(block["content"]))
    assert copyright_block is not date_block
    for fragment in ["CONTACT", "xinliu1969", "Technology, Qingdao", "Creative Commons", "Accepted Manuscript"]:
        matches = [block for block in pages[1] if block["bbox"][1] > 0.85 and fragment in _text(block["content"])]
        assert matches and all(block["type"] == "page_footnote" for block in matches)
    for page in pages[2:4]:
        assert len([block for block in page if block["type"] == "equation" and block["bbox"][1] > 0.89]) == 1
        assert not any(block["type"] == "footer" for block in page)


def test_real_three_rule_tables_have_complete_html_topology() -> None:
    """The header line wrap does not add records, and the two synchronization description columns each form five five-line merge cells."""
    pages = _pages("journal_layout_tables")
    tables = [next(block for block in pages[index] if block["type"] == "table") for index in (4, 5)]
    first, second = [BeautifulSoup(table["content"], "html.parser") for table in tables]
    assert len(first.select("tr")) == 6
    assert all(len(row.select("td,th")) == 8 for row in first.select("tr"))
    assert first.select("tr")[0].select("td,th")[-1].get_text() == "Correlation coefficient/%"
    assert len(second.select("tr")) == 26
    assert len(second.select("tr")[0].select("td,th")) == 6
    spans = second.select("[rowspan]")
    assert len(spans) == 10 and all(cell["rowspan"] == "5" for cell in spans)
    assert [cell.get_text() for cell in spans][1::2] == ["1047", "2744", "1722", "2839", "6261"]
    assert second.select("tr")[-1].get_text(" ", strip=True) == "DTU18 8.71 102.75 40.05"


def test_public_postprocess_preserves_title_and_rule_separated_paragraphs() -> None:
    """Exposed ModelJson to MiddleJson boundaries must not re-merge fixed independent paragraphs."""
    from copy import deepcopy
    from docvortex.api import postprocess
    from docvortex.schema import DocumentMetadata, ModelJson, Producer

    model = ModelJson(
        pages=deepcopy(_pages("journal_layout_tables")),
        page_index_map=[],
        metadata=DocumentMetadata(file_suffix="pdf", producer=Producer(name="test", version="1")),
    )
    result = postprocess(model)
    blocks = [block.model_dump(mode="json") for block in result.middle_json.pages[0].blocks]
    assert len([block for block in blocks if block["type"] == "doc_title"]) == 1
    copyright_block = next(block for block in blocks if _text(block["content"]).startswith("© 2024"))
    date_block = next(block for block in blocks if "Published online:" in _text(block["content"]))
    assert copyright_block is not date_block


def test_pdf_implicit_fill_closure_is_a_rule_but_triangle_is_not() -> None:
    """Verify implicit closure with real PDF draw operations to avoid simulating only intermediate objects."""
    stream = BytesIO()
    canvas = Canvas(stream, pagesize=(200, 200))
    canvas._code.extend(["20 150 m 180 150 l 180 149.5 l 20 149.5 l f*", "20 100 m 180 100 l 20 99.5 l f*"])
    canvas.showPage()
    canvas.save()
    with PDFDocument(stream.getvalue()) as document:
        rules = document.get_page_drawing_lines(0)
    assert len(rules) == 1
    assert rules[0].orientation == "horizontal"
    assert rules[0].bbox[1] == pytest.approx(50.0, abs=0.01)


def test_form_ink_fallback_preserves_external_caption_and_missing_evidence() -> None:
    """Only the actual glyphs are claimed if they are inside the container, no legends or missing glyph boxes are added."""
    line = _text_line("edge label", (20, 92, 70, 104), 0, ink_bbox=(21, 93, 69, 99))
    bbox = (10, 10, 90, 100)
    assert graphics._form_member_bbox(line, bbox) == line.ink_bbox
    assert graphics._form_member_bbox(replace(line, ink_bbox=(21, 101, 69, 107)), bbox) is None
    assert graphics._form_member_bbox(replace(line, ink_bbox=None), bbox) is None


@pytest.mark.parametrize(
    "pair,expected", [(("(2)", "(6)"), False), (("（12）", "（13）"), False), (("Journal 2024", "Journal 2025"), True)]
)
def test_formula_tags_do_not_supply_repeated_footer_evidence(pair: tuple[str, str], expected: bool) -> None:
    """Bracket numbers cannot be mistaken for repeated footers, and real footers containing text can still ignore changing numbers."""
    assert auxiliary_text._marginal_text_matches(*pair) is expected


def test_unmapped_ink_excludes_spaces_and_invisible_text() -> None:
    """Ordinary white space, synthetic line breaks, and invisible glyphs cannot expand formula screenshots."""
    char = {"char": " ", "tight_bbox": (10, 10, 15, 40), "font": {"size": 10}, "text_object_id": 1, "text_render_mode": 0}
    assert formulas._unmapped_formula_ink_bboxes([char]) == [(10, 10, 15, 40)]
    assert not formulas._unmapped_formula_ink_bboxes(
        [{**char, "tight_bbox": (10, 10, 15, 18)}, {**char, "text_object_id": None}, {**char, "text_render_mode": 3}]
    )


def test_fraction_rule_above_visible_ink_is_not_strikethrough() -> None:
    """When the fill fraction horizontal line passes through the font outline, it still needs to be checked whether it passes through the actual glyph."""
    from docvortex.analyzers.native.pdf.inline.detection import _build_line_candidate, _drawing_match_for_line
    from docvortex.analyzers.native.pdf.models import _AxisLine

    line = _text_line("abcd", (0, 0, 40, 20), 0)
    line.chars = [
        {"char": c, "bbox": (i * 10, 0, i * 10 + 10, 20), "tight_bbox": (i * 10, 12, i * 10 + 8, 18)}
        for i, c in enumerate("abcd")
    ]
    candidate = _build_line_candidate(line)
    assert candidate is not None
    rule = _AxisLine((0, 9.9, 40, 10.1), 0.2, "horizontal")
    assert _drawing_match_for_line(candidate, rule, "strikethrough") is None
    for char in line.chars:
        char["tight_bbox"] = (char["bbox"][0], 5, char["bbox"][2], 15)
    candidate = _build_line_candidate(line)
    assert candidate is not None and _drawing_match_for_line(candidate, rule, "strikethrough") is not None


@pytest.mark.parametrize(
    "name,index,fingerprint",
    [
        ("demo2.pdf", 2, "d10b184573a059cdbd8ccb13e2fd8d76eefcf8f73295fc386f29b96e017c43aa"),
        ("mixed_elements_pages_03_06.pdf", 1, "8662f48c81797c9ae3b7ffca69607538e08080f279545145e1b2c875e055c371"),
    ],
)
def test_reviewed_existing_formula_geometry(name: str, index: int, fingerprint: str) -> None:
    """Lock the complete geometry of the two pages for manual stacking frame acceptance, covering the same type of bracket cropping and repair."""
    from _flash_pdf_test_utils import _page_bbox_fingerprint, formula_detection_evidence

    source = Path(__file__).parents[2] / "demo" / "pdfs" / name
    with PDFDocument(str(source)) as document, formula_detection_evidence():
        pages = pipeline._analyze_native_document(document)
    assert _page_bbox_fingerprint(pages[index]) == fingerprint


@pytest.mark.parametrize("asymmetric,with_gaps", [(False, True), (True, False)])
def test_sparse_descriptor_ambiguity_keeps_recovery_fallback(asymmetric: bool, with_gaps: bool) -> None:
    """When description columns are out of sync or leaf columns have missing values, ordinary empty cells cannot be forcibly interpreted as merged cells."""
    from test_native_pdf_table import _char_items

    entries = [(f"H{col}", (col * 50 + 5, 5, col * 50 + 20, 12)) for col in range(6)]
    for row in range(9):
        for col in range(6):
            if col < 2 and row % 3 != (1 if asymmetric and col == 1 else 0):
                continue
            if with_gaps and col == 5 and row == 4:
                continue
            entries.append((f"{row}{col}", (col * 50 + 5, 25 + row * 12, col * 50 + 20, 32 + row * 12)))
    table = NativeTableInput(
        (0, 0, 300, 140),
        (300, 140),
        0,
        _char_items(entries),
        tuple(NativeTableRule((0, y, 300, y + 0.5), 0.5, "horizontal") for y in (0, 20, 139.5)),
    )
    from docvortex.analyzers.native.pdf._table_recovery.rule_band import build_rule_band_candidates
    from docvortex.analyzers.native.pdf._table_recovery.text import build_native_table_text

    text = build_native_table_text(table)
    assert text is not None
    assert build_rule_band_candidates(table, text) == []
