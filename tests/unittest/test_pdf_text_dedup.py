"""Verify that source-aware deduplication does not corrupt ligatures, standalone characters, and unique hidden OCR text."""

from __future__ import annotations

import copy
import math
from io import BytesIO

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
from reportlab.pdfgen.canvas import Canvas

from docvortex.document.pdf import PDFDocument
from docvortex.document.pdf.text._contracts import Bbox, Char
from docvortex.document.pdf.text.dedup import deduplicate_chars


def _char(
    text: str, x: float = 0, y: float = 0, *, obj: int | None = 0, mode: int | None = 0, width: float = 10, angle: float = 0
) -> Char:
    """Construct characters with true-source semantics to simulate different objects and rendering modes."""
    return {
        "char": text,
        "char_idx": 0,
        "source_indices": (0,),
        "bbox": Bbox([x, y, x + width, y + 12]),
        "origin": (x, y + 10),
        "font": {"name": "test", "flags": 0, "size": 12, "weight": 400},
        "rotation": -angle,
        "writing_angle": angle,
        "text_object_id": obj,
        "text_render_mode": mode,
    }


def _line(text: str, x: float = 0, y: float = 0, *, obj: int = 0, mode: int = 0) -> list[Char]:
    """Continuous glyphs are generated, and writing advancement and source index are maintained by each layer."""
    return [_char(c, x + i * 10, y, obj=obj, mode=mode) for i, c in enumerate(text)]


def _indexed(chars: list[Char]) -> list[Char]:
    """Assign continuous source indexes to complete character streams and do not allow test auxiliary data to fake the same glyph."""
    return [dict(c, char_idx=i, source_indices=(i,)) for i, c in enumerate(chars)]


def _text(chars: list[Char]) -> str:
    """Extract the complete character sequence from the result."""
    return "".join(c["char"] for c in chars)


@pytest.mark.parametrize("text", ["ff", "ffi", "ffl", "fi", "fl", "a\u0301", "人人", "𝜃𝜃", "AB"])
def test_same_glyph_mapping_is_preserved(text: str) -> None:
    """Multiple code values of the same glyph are all retained by default, and characters are not deleted through repeated letters or Chinese character whitelists."""
    chars = _indexed([_char(c) for c in text])
    assert _text(deduplicate_chars(chars)) == text


@pytest.mark.parametrize(
    "text,expected",
    [
        ("⼒力力", "力"),
        ("年年", "年"),
        ("⻓长", "长"),
        ("⻔门", "门"),
        ("⺠民", "民"),
        ("⾏行行", "行"),
        ("⻓門", "⻓門"),
        ("⼒力力", "力"),
    ],
)
def test_only_equivalent_han_mapping_is_collapsed(text: str, expected: str) -> None:
    """Different encodings must all be equivalent, and all original characters can still be traced after merging."""
    chars = _indexed([_char(c) for c in text])
    before = copy.deepcopy(chars)
    result = deduplicate_chars(chars)
    assert _text(result) == expected
    assert sorted({i for c in result for i in c["source_indices"]}) == list(range(len(text)))
    assert _text(chars) == _text(before)
    assert all(c["source_indices"] == b["source_indices"] for c, b in zip(chars, before))


@pytest.mark.parametrize("text", ["人人", "年年", "ff", "llll", "==", "∑∑", "⻓长"])
def test_distinct_origins_are_not_mapping_duplicates(text: str) -> None:
    """Even if the boxes of kerned characters nearly overlap, the independent origins must still be retained."""
    chars = _indexed([_char(c, i * 0.3, width=1) for i, c in enumerate(text)])
    assert _text(deduplicate_chars(chars)) == text


def test_missing_provenance_keeps_identical_characters() -> None:
    """Old guess deletion logic cannot be restored when source, origin or valid geometry is missing."""
    chars = _indexed([_char("f", obj=None), _char("f", obj=None), _char("年", obj=1), _char("年", obj=1)])
    for c in chars[2:]:
        c["origin"] = None
    assert _text(deduplicate_chars(chars)) == "ff年年"


@pytest.mark.parametrize("interleaved", [False, True])
@pytest.mark.parametrize("layers", [2, 3])
@pytest.mark.parametrize("offset", [(0, 0), (2.04, 2.04), (1.5, 0), (0, 1.5)])
def test_repeated_paint_layers_keep_one_complete_sequence(interleaved: bool, layers: int, offset: tuple[float, float]) -> None:
    """The two- and three-layer drawings interlaced with whole paragraphs and verbatim retain a complete copy of the text."""
    lines = [_line("ABCABC", n * offset[0], n * offset[1], obj=n) for n in range(layers)]
    chars = _indexed([c for group in (zip(*lines) if interleaved else lines) for c in group])
    result = deduplicate_chars(chars)
    assert _text(result) == "ABCABC"
    assert sorted({i for c in result for i in c["source_indices"]}) == list(range(len(chars)))
    assert all(c["text_object_id"] == 0 for c in result)
    assert _text(deduplicate_chars(result)) == "ABCABC"


def test_repeated_layers_with_ligatures_preserve_each_piece() -> None:
    """Three-segment code values of the same ligature must participate in repeat level matching together."""
    first = [_char(c) for c in "ffi"] + [_char("c", 10), _char("e", 20)]
    second = [dict(c, text_object_id=1) for c in first]
    result = deduplicate_chars(_indexed(first + second))
    assert _text(result) == "ffice"
    assert result[0]["source_indices"] == (0, 5)
    assert result[1]["source_indices"] == (1, 6)


@pytest.mark.parametrize("text", ["A", "AA", "AB"])
def test_offset_requires_continuous_varied_text(text: str) -> None:
    """Isolated homographies, monotonous repetitions, and shifts of less than three glyphs do not constitute shadow evidence."""
    chars = _indexed(_line(text) + _line(text, 2, 2, obj=1))
    assert _text(deduplicate_chars(chars)) == text * 2


def test_non_contiguous_offset_candidates_are_preserved() -> None:
    """Scattered identical characters cannot form a false shadow sequence."""
    a = [_char(c, i * 80) for i, c in enumerate("ABC")]
    b = [_char(c, i * 80 + 2, 2, obj=1) for i, c in enumerate("ABC")]
    assert _text(deduplicate_chars(_indexed(a + b))) == "ABCABC"


def test_unique_hidden_text_is_preserved() -> None:
    """The only hidden OCR layer on the scanned page and the same text in different locations cannot be deleted."""
    chars = _indexed(_line("OCR only", mode=3) + _line("Visible", y=100, obj=1))
    assert _text(deduplicate_chars(chars)) == "OCR onlyVisible"


@pytest.mark.parametrize(
    "visible,hidden,removed",
    [
        ("建筑物理与设备", "莲筑物理与设备", True),
        ("九门提督带你精读设备", "力门提督带你精读设备", True),
        ("GB 50736-2012", "GB50736-2012", True),
        ("第1条规范", "第2条规范", False),
        ("some text", "some texts", False),
        ("some text", "same text", False),
        ("规范", "现范", False),
        ("规范文件", "规范文件补充", False),
    ],
)
def test_hidden_requires_visible_content_and_conservative_match(visible: str, hidden: str, removed: bool) -> None:
    """Hidden copies allow explicit Chinese character OCR substitutions and do not allow differences in numbers, letters, or additions or deletions."""
    # Spaces do not occupy glyphs, simulating the difference in space encoding between hidden OCR and visible text.
    visible_chars = _line(visible.replace(" ", ""))
    hidden_chars = _line(hidden.replace(" ", ""), obj=1, mode=3)
    for c in hidden_chars:
        c["font"] = {**c["font"], "name": "OCR font"}
    result = deduplicate_chars(_indexed(visible_chars + hidden_chars))
    assert _text(result) == visible.replace(" ", "") + ("" if removed else hidden.replace(" ", ""))


def test_hidden_partial_overlap_and_adjacent_cells_are_preserved() -> None:
    """Undercoverage and the same text in adjacent cells cannot suppress hidden content."""
    for x in (7, 100):
        chars = _indexed(_line("ABC") + _line("ABC", x=x, obj=1, mode=3))
        assert _text(deduplicate_chars(chars)) == "ABCABC"


def test_rotated_hidden_duplicate_and_page_regions() -> None:
    """Text axis projection supports rotating a copy while preserving the main text in another position."""
    chars = _indexed(_line("ABCD") + _line("ABCD", obj=1, mode=3) + _line("ABCD", y=100, obj=2))
    for c in chars:
        x0, y0, x1, y1 = c["bbox"]
        c["bbox"] = Bbox([200 - y1, x0, 200 - y0, x1])
        x, y = c["origin"]
        c["origin"] = (200 - y, x)
        c["writing_angle"] = math.pi / 2
        c["rotation"] = 0
    assert _text(deduplicate_chars(chars)) == "ABCDABCD"


def _mapping_pdf(mapping: str) -> bytes:
    """Construct a real ToUnicode one-to-many mapping and verify PDFium extraction with two public character entries."""
    output = BytesIO()
    canvas = Canvas(output, pagesize=(200, 100))
    canvas.drawString(20, 50, "X")
    canvas.save()
    reader = PdfReader(BytesIO(output.getvalue()))
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    font = writer.pages[0]["/Resources"]["/Font"]["/F1"].get_object()
    cmap = DecodedStreamObject()
    target = mapping.encode("utf-16-be").hex()
    cmap.set_data(
        f"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def /CMapName /Test def /CMapType 2 def 1 begincodespacerange <00><FF> endcodespacerange 1 beginbfchar <58><{target}> endbfchar endcmap CMapName currentdict /CMap defineresource pop end end".encode()
    )
    font[NameObject("/ToUnicode")] = writer._add_object(cmap)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize(
    "mapping,expected", [("ffi", "ffi"), ("ﬃ", "ffi"), ("ff", "ff"), ("⼒力力", "力"), ("⻓长", "长"), ("𝜃", "𝜃")]
)
def test_real_pdfium_mapping_and_geometry_interfaces(mapping: str, expected: str) -> None:
    """The base and extended interfaces output the same semantics, ligatures per segment and surrogate source pairs are preserved."""
    with PDFDocument(_mapping_pdf(mapping)) as document:
        base = document.get_page_chars(0)
        extended = document.get_page_chars_with_geometry(0)
    assert _text(base) == expected
    assert _text(extended.chars) == expected
    assert [(c["char_idx"], c["source_indices"], list(c["bbox"])) for c in base] == [
        (c["char_idx"], c["source_indices"], list(c["bbox"])) for c in extended.chars
    ]
    assert all(type(c["text_object_id"]) is int for c in base)
    assert all(c["char_idx"] in extended.origins for c in extended.chars)


def test_recorded_badcase_characters() -> None:
    """Replay minimal character snippets of five real-life examples without the need for an external hard drive or full business documentation."""
    import json
    from pathlib import Path

    # Fixed UTF-8 to prevent Windows default encoding from decoding a single Chinese character into multiple characters.
    fixtures = json.loads((Path(__file__).parents[1] / "fixtures/pdf_char_dedup.json").read_text(encoding="utf-8"))
    for case in fixtures:
        chars = [{**c, "bbox": Bbox(c["bbox"]), "source_indices": tuple(c["source_indices"])} for c in case["chars"]]
        assert _text(deduplicate_chars(chars)) == case["expected"], case["name"]


def test_pdfium_shear_angle_does_not_change_writing_direction() -> None:
    """True PDF Artificial italic clipping should not be treated as a rotation of the writing baseline."""
    output = BytesIO()
    canvas = Canvas(output, pagesize=(300, 150))
    canvas.transform(1, 0, 1 / 3, 1, 0, 0)
    canvas.drawString(20, 80, "SHEAR BASELINE")
    canvas.save()
    with PDFDocument(output.getvalue()) as document:
        chars = document.get_page_chars(0)
    assert any(abs(c["rotation"]) > 0.1 for c in chars)
    assert all(abs(c["writing_angle"]) < 0.001 for c in chars if c["char"].strip())


def test_removed_hidden_paragraph_leaves_no_orphaned_lines() -> None:
    """The white space between deleted copies is cleared, and the original text paragraph separation remains."""
    chars = _indexed(
        _line("ABC") + [_char("\r"), _char("\n")] + _line("ABC", obj=1, mode=3) + [_char("\r", obj=None), _char("\n", obj=None)]
    )
    assert _text(deduplicate_chars(chars)) == "ABC\r\n"


def test_transparent_text_cannot_suppress_unique_hidden_ocr() -> None:
    """Text in fill mode but with zero transparency cannot be treated as visible original text."""
    visible = _line("ABCD")
    for c in visible:
        c["text_is_visible"] = False
    chars = _indexed(visible + _line("ABCD", obj=1, mode=3))
    assert _text(deduplicate_chars(chars)) == "ABCDABCD"


def test_pdfium_reports_transparent_objects_without_losing_ocr() -> None:
    """Read object transparency from real PDF and keep unique hidden text next to it."""
    output = BytesIO()
    canvas = Canvas(output, pagesize=(300, 150))
    canvas.setFillAlpha(0)
    canvas.drawString(20, 80, "TRANSPARENT")
    canvas.setFillAlpha(1)
    text = canvas.beginText(20, 40)
    text.setTextRenderMode(3)
    text.textOut("OCR ONLY")
    canvas.drawText(text)
    canvas.save()
    with PDFDocument(output.getvalue()) as document:
        chars = document.get_page_chars(0)
    assert "OCR ONLY" in _text(chars)
    assert all(not c.get("text_is_visible") for c in chars)


def test_shadow_evidence_cannot_skip_different_interior_characters() -> None:
    """When there are different texts between three identical letters, you cannot skip the differences and delete part of the original text."""
    chars = _indexed(_line("AxBxC") + _line("AyByC", 1, 1, obj=1))
    for c in chars:
        b = c["bbox"]
        c["bbox"] = Bbox([b[0] * 0.4, b[1], b[2] * 0.4, b[3]])
        x, y = c["origin"]
        c["origin"] = (x * 0.4, y)
    assert _text(deduplicate_chars(chars)) == "AxBxCAyByC"


def test_three_layers_of_two_glyphs_are_still_insufficient_evidence() -> None:
    """Three-layered characters still only have two independent glyphs, so the number of copies cannot be relied upon to provide continuous evidence."""
    layers = [_line("AB", i * 1.5, obj=i) for i in range(3)]
    chars = _indexed([c for group in zip(*layers) for c in group])
    assert len(deduplicate_chars(chars)) == 6


def test_invalid_and_extreme_geometry_is_conservatively_preserved() -> None:
    """Non-limited directions, zero area, and extremely large hidden boxes will not accidentally delete text or enumerate infinite empty grids."""
    cases = [_char("A", obj=1, mode=3), _char("A", obj=1, mode=3), _char("A", obj=1, mode=3)]
    cases[0]["writing_angle"] = float("nan")
    cases[1]["bbox"] = Bbox([0, 0, 0, 0])
    cases[2]["bbox"] = Bbox([-1e12, -1e12, 1e12, 1e12])
    for other in cases:
        assert _text(deduplicate_chars(_indexed([_char("A"), other]))) == "AA"
