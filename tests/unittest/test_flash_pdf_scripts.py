from __future__ import annotations

import sys
from collections.abc import Iterator
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from docvortex.analyzers.native.pdf._script_geometry import ScriptRole
from docvortex.analyzers.native.pdf.geometry import _rotate_bbox_from_upright
from docvortex.analyzers.native.pdf.inline.materialize import (
    apply_pdf_text_links,
    apply_pdf_text_scripts,
    apply_pdf_text_styles,
    materialize_pdf_inline_spans,
)
from docvortex.analyzers.native.pdf.inline.scripts import _refine_math_script_tokens, detect_pdf_text_script_lines
from docvortex.analyzers.native.pdf.inline.types import (
    PDFTextLinkLine,
    PDFTextLinkRange,
    PDFTextScriptLine,
    PDFTextScriptRange,
    PDFTextStyleLine,
    PDFTextStyleRange,
)
from docvortex.analyzers.native.pdf.models import _AxisLine, _LineItem
from docvortex.analyzers.native.pdf.pipeline import _analyze_native_document
from _flash_pdf_test_utils import formula_detection_evidence
from docvortex.document.pdf._document import PDFDocument
from docvortex.document.pdf.text._contracts import Bbox, Char
from docvortex.schema import BBox

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEMO_PDF_DIR = _PROJECT_ROOT / "demo" / "pdfs"

_REVIEWED_SCRIPT_EXPECTATIONS = {
    "demo1.pdf": {
        (3, 45): (("2", "superscript"), ("2", "superscript")),
        (3, 50): (("2", "superscript"),),
    },
    "demo3.pdf": {
        (1, 74): (("1", "subscript"), ("2", "subscript"), ("n", "subscript")),
        (3, 5): (("i", "subscript"), ("j", "subscript")),
        (3, 7): (("i", "subscript"),),
        (3, 8): (("j", "subscript"), ("i", "subscript"), ("j", "subscript")),
        (3, 13): (("i", "subscript"), ("j", "subscript")),
        (3, 60): (),
    },
    "中文论文2.pdf": {
        (7, 46): (("1", "subscript"),),
        (7, 47): (("1", "subscript"),),
        (7, 80): (),
        (7, 82): (("m", "subscript"),),
        (7, 83): (("m", "subscript"),),
        (7, 87): (("i", "subscript"), ("i", "subscript")),
        (7, 88): (("m", "subscript"),),
        (8, 7): (("i", "subscript"),),
        (8, 8): (("m", "subscript"),),
        (8, 14): (),
        (8, 22): (),
        (10, 25): (("i", "subscript"),),
        (10, 26): (),
        (10, 27): (("[112]", "superscript"),),
        (11, 35): (("[127]", "superscript"), ("[128]", "superscript"), ("[129]", "superscript")),
        (11, 56): (
            ("1", "subscript"),
            ("2", "subscript"),
            ("n", "subscript"),
            ("1", "subscript"),
            ("2", "subscript"),
            ("m", "subscript"),
        ),
        (11, 66): (("1", "subscript"),),
        (11, 73): (("Score", "subscript"),),
        (11, 92): (),
        (11, 94): (),
        (11, 97): (),
    },
    "IEBM_A_2667169_O-5.pdf": {
        (0, 3): (),
        (0, 7): (),
        (0, 11): (),
        (0, 14): (),
    },
    "mixed_elements_pages_11_15.pdf": {
        (0, 30): (("2", "subscript"),),
    },
}

_RUN_CLOSURE_EXPECTATIONS = {
    "demo3.pdf": {
        (6, 38): (("BASE-SAT", "subscript"),),
        (6, 102): (("BASE-SO", "subscript"),),
        (6, 114): (("BASE-SO", "subscript"),),
    },
    "demo2.pdf": {
        (2, 22): (("t-1", "subscript"),),
        (2, 25): (("t-1", "subscript"),),
    },
    "mixed_elements_pages_03_06.pdf": {
        (0, 22): (("–3", "superscript"),),
        (0, 64): (("6.6", "subscript"),),
        (1, 25): (("–7", "superscript"),),
        (1, 26): (("–5", "superscript"),),
        (3, 19): (("1–x", "subscript"),),
    },
    "mixed_elements_pages_11_15.pdf": {
        (3, 156): (
            ("a)", "superscript"),
            ("b)", "superscript"),
            ("c)", "superscript"),
            ("d)", "superscript"),
        ),
    },
    "mixed_elements_pages_39_40.pdf": {
        (1, 44): (("i−1", "subscript"), ("i+1", "subscript")),
        (1, 46): (("i−1", "subscript"), ("i+1", "subscript")),
    },
}

_NO_SCRIPT_SOURCE_EXPECTATIONS = {
    "中文论文2.pdf": {
        (15, 15),
        (15, 35),
        (15, 37),
    }
}

_GENERAL_RECOVERY_EXPECTATIONS = {
    "demo3.pdf": {
        (4, 51): (("2", "superscript"),),
        (4, 163): (("3", "superscript"),),
        (8, 48): (("5", "superscript"),),
    },
    "中文论文.pdf": {
        (3, 95): (("2", "superscript"),),
    },
    "中文论文2.pdf": {
        (0, 41): (("[2]", "superscript"), ("[3]", "superscript")),
        (2, 95): (("[12]", "superscript"),),
        (3, 14): (("[17]", "superscript"),),
        (3, 83): (("[30]", "superscript"),),
        (3, 87): (("[31]", "superscript"), ("[32]", "superscript"), ("[33]", "superscript")),
        (13, 69): (("[41]", "superscript"),),
    },
}


def _origin_from_upright(
    origin: tuple[float, float],
    page_size: tuple[float, float],
    angle: int,
) -> tuple[float, float]:
    """Inversely transform local forward origin to page coordinates."""
    x, y = origin
    page_width, page_height = page_size
    if angle == 270:
        return y, page_height - x
    if angle == 90:
        return page_width - y, x
    if angle == 180:
        return page_width - x, page_height - y
    return origin


def _script_fixture(
    *,
    angle: int = 0,
    formula_region: bool = True,
) -> tuple[_LineItem, dict[int, BBox], dict[int, tuple[float, float]], tuple[float, float]]:
    """Constructs a D-i-p local formula row containing both stable superscripts and subscripts."""
    page_size = (100.0, 120.0)
    local_bboxes = (
        (10.0, 40.0, 20.0, 50.0),
        (20.0, 34.0, 26.0, 40.0),
        (20.0, 50.0, 26.0, 56.0),
    )
    local_origins = ((10.0, 49.0), (20.0, 40.0), (20.0, 56.0))
    page_bboxes = tuple(_rotate_bbox_from_upright(bbox, page_size, angle) for bbox in local_bboxes)
    page_origins = tuple(_origin_from_upright(origin, page_size, angle) for origin in local_origins)
    chars: list[Char] = [
        {
            "char": text,
            "char_idx": index,
            "bbox": Bbox(list(page_bboxes[index])),
            "rotation": 0.0,
            "font": {},
        }
        for index, text in enumerate("Dip")
    ]
    line = _LineItem(
        text="Dip",
        bbox=(
            min(bbox[0] for bbox in page_bboxes),
            min(bbox[1] for bbox in page_bboxes),
            max(bbox[2] for bbox in page_bboxes),
            max(bbox[3] for bbox in page_bboxes),
        ),
        angle=angle,
        source_index=0,
        chars=chars,
        inline_math_regions=[
            (
                min(bbox[0] for bbox in page_bboxes),
                min(bbox[1] for bbox in page_bboxes),
                max(bbox[2] for bbox in page_bboxes),
                max(bbox[3] for bbox in page_bboxes),
            )
        ]
        if formula_region
        else [],
    )
    return (
        line,
        dict(enumerate(page_bboxes)),
        dict(enumerate(page_origins)),
        page_size,
    )


def _compact_refinement_fixture(text: str) -> tuple[list[Char], dict[int, BBox], dict[int, tuple[float, float]]]:
    """Construct a compact token refined fixture on the same tight/origin baseline."""
    chars: list[Char] = []
    tight_bboxes: dict[int, BBox] = {}
    origins: dict[int, tuple[float, float]] = {}
    for index, char_text in enumerate(text):
        left = 10.0 + index * 6.0
        bbox = (left, 40.0, left + 5.0, 46.0)
        chars.append(
            {
                "char": char_text,
                "char_idx": index,
                "bbox": Bbox(list(bbox)),
                "rotation": 0.0,
                "font": {},
            }
        )
        tight_bboxes[index] = bbox
        origins[index] = (left, 46.0)
    return chars, tight_bboxes, origins


def test_flash_compact_aligned_script_suffix_closes_complete_run() -> None:
    """Verifying that the trusted BASE subscript will completely close the ``-SAT`` with the same baseline."""
    chars, tight_bboxes, origins = _compact_refinement_fixture("XBASE-SAT")

    roles = _refine_math_script_tokens(
        chars,
        ["body", *(["sub"] * len("BASE-SAT"))],
        tight_bboxes,
        origins,
        formula_region=False,
    )

    assert roles == ["body", *(["sub"] * len("BASE-SAT"))]


@pytest.mark.parametrize(
    ("text", "raw_roles"),
    [
        pytest.param("BASE-SAT", ["sub"] * len("BASE-SAT"), id="missing-body-anchor"),
        pytest.param("MODEL-SAT", ["body"] * len("MODEL-SAT"), id="plain-hyphenated-word"),
    ],
)
def test_flash_compact_script_suffix_requires_anchor_and_raw_geometry(
    text: str,
    raw_roles: list[ScriptRole],
) -> None:
    """Hyphenated words will not be closed when validating without text anchors or original subscript evidence."""
    chars, tight_bboxes, origins = _compact_refinement_fixture(text)

    roles = _refine_math_script_tokens(
        chars,
        raw_roles,
        tight_bboxes,
        origins,
        formula_region=False,
    )

    assert roles == ["body"] * len(text)


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_flash_script_geometry_uses_upright_coordinates(angle: int) -> None:
    """Verify that the roles of loose/tight/origin are consistent after synchronization and forwarding in four page directions."""
    line, tight_bboxes, origins, page_size = _script_fixture(angle=angle)

    script_lines = detect_pdf_text_script_lines(
        [line],
        page_size,
        tight_bboxes,
        origins,
    )

    assert [(item.start, item.end, item.style, item.formula_region) for item in script_lines[0].script_ranges] == [
        (1, 2, "superscript", True),
        (2, 3, "subscript", True),
    ]


def test_flash_formula_region_rebases_before_classification() -> None:
    """The validation formula area uses the internal D baseline while stabilizing the output i superscript and p subscript."""
    line, tight_bboxes, origins, page_size = _script_fixture()

    script_line = detect_pdf_text_script_lines(
        [line],
        page_size,
        tight_bboxes,
        origins,
    )[0]

    assert all(item.stable_body_count == 1 for item in script_line.script_ranges)
    assert [item.style for item in script_line.script_ranges] == [
        "superscript",
        "subscript",
    ]


def test_flash_formula_region_without_internal_body_stays_plain() -> None:
    """style is not output when verifying that all characters in the formula area are offset synchronously and there is no internal text baseline."""
    line, tight_bboxes, origins, page_size = _script_fixture()
    line = replace(line, chars=line.chars[1:], text="ip")
    line.inline_math_regions = [line.bbox]

    script_line = detect_pdf_text_script_lines(
        [line],
        page_size,
        tight_bboxes,
        origins,
    )[0]

    assert script_line.script_ranges == ()


def test_flash_missing_extended_geometry_stays_plain() -> None:
    """Verify that Flash does not pass loose when tight/origin is missing bbox guesses superscript and subscript."""
    line, _tight_bboxes, _origins, page_size = _script_fixture()

    script_line = detect_pdf_text_script_lines([line], page_size, {}, {})[0]

    assert script_line.script_ranges == ()


def test_flash_fraction_bar_suppresses_stacked_script_candidate() -> None:
    """Verify that when overlapping words are separated by dashes, superscripts and subscripts will be rejected as whole fractions."""
    page_size = (100.0, 120.0)
    line_chars: list[Char] = [
        {"char": "x", "char_idx": 0, "bbox": Bbox([10.0, 40.0, 20.0, 50.0]), "rotation": 0.0, "font": {}},
        {"char": "1", "char_idx": 1, "bbox": Bbox([22.0, 32.0, 30.0, 40.0]), "rotation": 0.0, "font": {}},
    ]
    denominator: Char = {
        "char": "M",
        "char_idx": 2,
        "bbox": Bbox([22.0, 44.0, 30.0, 52.0]),
        "rotation": 0.0,
        "font": {},
    }
    line = _LineItem(
        text="x1",
        bbox=(10.0, 32.0, 30.0, 52.0),
        angle=0,
        source_index=0,
        chars=line_chars,
    )
    tight_bboxes = {
        0: (10.0, 40.0, 20.0, 50.0),
        1: (22.0, 32.0, 30.0, 40.0),
        2: (22.0, 44.0, 30.0, 52.0),
    }
    origins = {0: (10.0, 49.0), 1: (22.0, 40.0), 2: (22.0, 52.0)}

    unfiltered = detect_pdf_text_script_lines([line], page_size, tight_bboxes, origins)[0]
    filtered = detect_pdf_text_script_lines(
        [line],
        page_size,
        tight_bboxes,
        origins,
        all_chars=[*line_chars, denominator],
        drawing_lines=[_AxisLine((21.0, 41.0, 31.0, 42.0), 1.0, "horizontal")],
    )[0]

    assert [(item.start, item.end, item.style) for item in unfiltered.script_ranges] == [(1, 2, "superscript")]
    assert filtered.script_ranges == ()


def test_flash_long_text_separator_does_not_suppress_leading_note_marker() -> None:
    """Verify that text on either side of a long footnote separator is not misinterpreted as a partial formula."""

    page_size = (100.0, 100.0)
    below_chars: list[Char] = []
    tight_bboxes: dict[int, BBox] = {}
    origins: dict[int, tuple[float, float]] = {}
    for index, text in enumerate("2Word"):
        left = 10.0 + index * 7.0
        bbox = (left, 43.0, left + (4.0 if index == 0 else 6.0), 47.0 if index == 0 else 51.0)
        below_chars.append({"char": text, "char_idx": index, "bbox": Bbox(list(bbox)), "rotation": 0.0, "font": {}})
        tight_bboxes[index] = bbox
        origins[index] = (left, bbox[3])
    above_chars: list[Char] = []
    for offset, text in enumerate("data", start=len(below_chars)):
        left = 10.0 + (offset - len(below_chars)) * 7.0
        bbox = (left, 28.0, left + 6.0, 34.0)
        above_chars.append({"char": text, "char_idx": offset, "bbox": Bbox(list(bbox)), "rotation": 0.0, "font": {}})
        tight_bboxes[offset] = bbox
        origins[offset] = (left, 34.0)
    line = _LineItem(
        text="2Word",
        bbox=(10.0, 43.0, 44.0, 51.0),
        angle=0,
        source_index=0,
        chars=below_chars,
    )

    script_line = detect_pdf_text_script_lines(
        [line],
        page_size,
        tight_bboxes,
        origins,
        all_chars=[*above_chars, *below_chars],
        drawing_lines=[_AxisLine((5.0, 39.8, 65.0, 40.2), 0.4, "horizontal")],
    )[0]

    assert [(item.start, item.end, item.style) for item in script_line.script_ranges] == [(0, 1, "superscript")]


@pytest.mark.parametrize(
    ("text", "script_start", "expected_text"),
    [
        pytest.param("R2", 1, "2", id="adjacent-base"),
        pytest.param("A[2]", 1, "[2]", id="numeric-citation"),
    ],
)
def test_flash_restored_formula_line_preserves_strong_structural_scripts(
    text: str,
    script_start: int,
    expected_text: str,
) -> None:
    """Verify that the recovery formula line yields only weak token and preserves the locally geometrically stable strong script structure."""

    chars: list[Char] = []
    tight_bboxes: dict[int, BBox] = {}
    origins: dict[int, tuple[float, float]] = {}
    for index, char_text in enumerate(text):
        left = 10.0 + index * 7.0
        bbox = (left, 40.0, left + 6.0, 50.0) if index < script_start else (left, 34.0, left + 4.0, 40.0)
        chars.append({"char": char_text, "char_idx": index, "bbox": Bbox(list(bbox)), "rotation": 0.0, "font": {}})
        tight_bboxes[index] = bbox
        origins[index] = (left, bbox[3])
    line_bbox = (10.0, 34.0, 10.0 + len(text) * 7.0, 50.0)
    line = _LineItem(
        text=text,
        bbox=line_bbox,
        angle=0,
        source_index=0,
        chars=chars,
        restored_inline_cluster=True,
        inline_math_regions=[line_bbox],
    )

    script_line = detect_pdf_text_script_lines(
        [line],
        (100.0, 100.0),
        tight_bboxes,
        origins,
    )[0]

    assert [(script_line.text[item.start : item.end], item.style) for item in script_line.script_ranges] == [
        (expected_text, "superscript")
    ]


@pytest.mark.parametrize("text", ["Bm", "r1"])
def test_flash_formula_region_marks_only_the_index(text: str) -> None:
    """The B_m and r_1 inside the verification formula only mark the downward index, and do not mark the entire token as a subscript."""
    line, tight_bboxes, origins, page_size = _script_fixture()
    body_char = {**line.chars[0], "char": text[0]}
    index_char = {**line.chars[2], "char": text[1]}
    line = replace(line, text=text, chars=[body_char, index_char])

    script_line = detect_pdf_text_script_lines(
        [line],
        page_size,
        tight_bboxes,
        origins,
    )[0]

    assert [(item.start, item.end, item.style) for item in script_line.script_ranges] == [(1, 2, "subscript")]


def test_flash_script_projection_combines_font_styles() -> None:
    """Verify that the Flash superscript and subscript are combined in the same InlineSpan stream with the existing bold range."""
    blocks = [
        {
            "type": "text",
            "bbox": [0.0, 0.0, 1.0, 1.0],
            "content": "Bm",
        }
    ]
    style_line = PDFTextStyleLine(
        bbox=(10.0, 10.0, 30.0, 20.0),
        text="Bm",
        style_ranges=(PDFTextStyleRange(0, 1, ("bold",)),),
        source_index=0,
    )
    script_line = PDFTextScriptLine(
        bbox=(10.0, 10.0, 30.0, 20.0),
        text="Bm",
        script_ranges=(
            PDFTextScriptRange(
                1,
                2,
                "subscript",
                (20.0, 15.0, 25.0, 20.0),
                1,
                True,
            ),
        ),
        source_index=0,
        angle=0,
    )

    apply_pdf_text_styles(blocks, [style_line], (100.0, 100.0))
    apply_pdf_text_scripts(blocks, [script_line], (100.0, 100.0))
    materialize_pdf_inline_spans(blocks)

    assert blocks[0]["content"] == [
        {"type": "text", "content": "B", "styles": ["bold"]},
        {"type": "text", "content": "m", "styles": ["subscript"]},
    ]


def test_flash_script_projection_does_not_leak_to_same_text() -> None:
    """Candidates in the verification formula are projected according to the entire line interval, and will not fall back to ordinary characters with the same name in the same line."""
    blocks = [
        {
            "type": "text",
            "bbox": [0.0, 0.0, 1.0, 1.0],
            "content": "Bm Bm",
        }
    ]
    script_line = PDFTextScriptLine(
        bbox=(10.0, 10.0, 90.0, 20.0),
        text="BmBm",
        script_ranges=(PDFTextScriptRange(3, 4, "subscript", (70.0, 15.0, 75.0, 20.0), 1, True),),
        source_index=0,
        angle=0,
    )

    apply_pdf_text_scripts(blocks, [script_line], (100.0, 100.0))
    materialize_pdf_inline_spans(blocks)

    assert blocks[0]["content"] == [
        {"type": "text", "content": "Bm B"},
        {"type": "text", "content": "m", "styles": ["subscript"]},
    ]


def test_flash_script_projection_ignores_unrelated_line_cursor() -> None:
    """Validating extraneous short lines does not advance shared cursor and swallow up subsequent fully referenced scripts."""

    blocks = [{"type": "text", "bbox": [0.0, 0.0, 1.0, 1.0], "content": "[12]"}]
    lines = [
        PDFTextScriptLine(
            bbox=(10.0, 10.0, 20.0, 20.0),
            text="[",
            script_ranges=(PDFTextScriptRange(0, 1, "superscript", (10.0, 10.0, 12.0, 15.0), 1, False),),
            source_index=0,
            angle=0,
        ),
        PDFTextScriptLine(
            bbox=(10.0, 10.0, 40.0, 20.0),
            text="[12]",
            script_ranges=(PDFTextScriptRange(0, 4, "superscript", (10.0, 10.0, 30.0, 15.0), 1, False),),
            source_index=1,
            angle=0,
        ),
    ]

    apply_pdf_text_scripts(blocks, lines, (100.0, 100.0))
    materialize_pdf_inline_spans(blocks)

    assert blocks[0]["content"] == [{"type": "text", "content": "[12]", "styles": ["superscript"]}]


def test_flash_script_projection_combines_hyperlink() -> None:
    """Verify that subscript Span order and URL are preserved when hyperlinks are comaterialized with superscript and subscript intervals."""
    blocks = [
        {
            "type": "text",
            "bbox": [0.0, 0.0, 1.0, 1.0],
            "content": "x2",
        }
    ]
    link_line = PDFTextLinkLine(
        bbox=(10.0, 10.0, 30.0, 20.0),
        text="x2",
        link_ranges=(PDFTextLinkRange(0, 2, "https://example.test/x2"),),
        source_index=0,
    )
    script_line = PDFTextScriptLine(
        bbox=link_line.bbox,
        text="x2",
        script_ranges=(PDFTextScriptRange(1, 2, "superscript", (20.0, 10.0, 25.0, 15.0), 1, False),),
        source_index=0,
        angle=0,
    )

    apply_pdf_text_links(blocks, [link_line], (100.0, 100.0))
    apply_pdf_text_scripts(blocks, [script_line], (100.0, 100.0))
    materialize_pdf_inline_spans(blocks)

    assert blocks[0]["content"] == [
        {
            "type": "hyperlink",
            "content": [
                {"type": "text", "content": "x", "styles": []},
                {"type": "text", "content": "2", "styles": ["superscript"]},
            ],
            "url": "https://example.test/x2",
        }
    ]


@pytest.mark.parametrize("block_type", ["table", "code", "equation", "image"])
def test_flash_scripts_exclude_non_natural_blocks(block_type: str) -> None:
    """Validation tables, codes, stand-alone formulas, and image containers do not receive subscripts or subscripts Flash."""
    blocks = [
        {
            "type": block_type,
            "bbox": [0.0, 0.0, 1.0, 1.0],
            "content": "Bm",
        }
    ]
    script_line = PDFTextScriptLine(
        bbox=(10.0, 10.0, 30.0, 20.0),
        text="Bm",
        script_ranges=(
            PDFTextScriptRange(
                1,
                2,
                "subscript",
                (20.0, 15.0, 25.0, 20.0),
                1,
                False,
            ),
        ),
        source_index=0,
        angle=0,
    )

    apply_pdf_text_scripts(blocks, [script_line], (100.0, 100.0))
    materialize_pdf_inline_spans(blocks)

    assert blocks[0]["content"] == "Bm"


def test_late_inline_math_region_drops_unrebased_candidate() -> None:
    """Validation of subsequently recovered formula regions filters out candidates that are not rebaselined within the formula."""
    blocks = [
        {
            "type": "text",
            "bbox": [0.0, 0.0, 1.0, 1.0],
            "content": "Bm",
            "_inline_math_regions": [[0.15, 0.1, 0.3, 0.3]],
        }
    ]
    script_line = PDFTextScriptLine(
        bbox=(10.0, 10.0, 30.0, 30.0),
        text="Bm",
        script_ranges=(
            PDFTextScriptRange(
                1,
                2,
                "subscript",
                (20.0, 15.0, 25.0, 20.0),
                1,
                False,
            ),
        ),
        source_index=0,
        angle=0,
    )

    apply_pdf_text_scripts(blocks, [script_line], (100.0, 100.0))
    materialize_pdf_inline_spans(blocks)

    assert blocks[0]["content"] == [{"type": "text", "content": "Bm"}]
    assert "_inline_math_regions" not in blocks[0]


@lru_cache(maxsize=None)
def _flash_script_analysis(
    pdf_name: str,
) -> tuple[tuple[tuple[dict[str, Any], ...], ...], tuple[dict[str, Any], ...]]:
    """Parse real Flash PDF at once, formula detection evidence and line-by-line script diagnostics before cache flush."""

    diagnostics: list[dict[str, Any]] = []
    with PDFDocument(str(_DEMO_PDF_DIR / pdf_name)) as document, formula_detection_evidence():
        pages = _analyze_native_document(document, script_diagnostics=diagnostics)
    return tuple(tuple(page) for page in pages), tuple(diagnostics)


def _flash_script_runs(pdf_name: str) -> list[tuple[str, tuple[str, ...]]]:
    """Collects the final TextSpan superscript and subscript from the cached real Flash page."""

    pages, _diagnostics = _flash_script_analysis(pdf_name)
    runs: list[tuple[str, tuple[str, ...]]] = []

    def walk(value: Any) -> None:
        """Recursively collect final TextSpan style."""
        if isinstance(value, dict):
            styles = tuple(str(style) for style in value.get("styles", []))
            content = value.get("content")
            if value.get("type") == "text" and isinstance(content, str) and {"superscript", "subscript"}.intersection(styles):
                runs.append((content, styles))
            for child in value.values():
                walk(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child)

    walk(pages)
    return runs


def _flash_script_diagnostics(pdf_name: str) -> tuple[dict[str, Any], ...]:
    """Return line-by-line superscript sidecar in the unified analysis cache."""

    _pages, diagnostics = _flash_script_analysis(pdf_name)
    return diagnostics


@pytest.fixture(scope="module", autouse=True)
def _clear_real_script_analysis_cache_after_module() -> Iterator[None]:
    """Release the real PDF page and script diagnostic cache after the module ends."""

    yield
    _flash_script_analysis.cache_clear()


def test_real_script_pages_and_diagnostics_share_one_analysis(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verification final script span shares the same real document analysis with line-by-line diagnostics."""

    analyze_calls: list[object] = []

    class FakePDFDocument:
        """Provides the minimal context required for script cache testing."""

        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> FakePDFDocument:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def fake_analyze(
        document: object,
        *,
        script_diagnostics: list[dict[str, Any]],
    ) -> list[list[dict[str, Any]]]:
        """Log analysis and construct pages and diagnostics simultaneously."""

        analyze_calls.append(document)
        script_diagnostics.append(
            {
                "script_lines": [],
                "materialized_ranges": [],
            }
        )
        return [
            [
                {
                    "type": "text",
                    "content": [
                        {
                            "type": "text",
                            "content": "2",
                            "styles": ["superscript"],
                        }
                    ],
                }
            ]
        ]

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "PDFDocument", FakePDFDocument)
    monkeypatch.setattr(module, "_analyze_native_document", fake_analyze)
    monkeypatch.setattr(module, "_DEMO_PDF_DIR", tmp_path)

    diagnostics = _flash_script_diagnostics("cache-fixture.pdf")
    runs = _flash_script_runs("cache-fixture.pdf")

    assert len(analyze_calls) == 1
    assert diagnostics == ({"script_lines": [], "materialized_ranges": []},)
    assert runs == [("2", ("superscript",))]


@pytest.mark.parametrize("pdf_name", tuple(_REVIEWED_SCRIPT_EXPECTATIONS))
def test_user_reviewed_flash_script_ranges(pdf_name: str) -> None:
    """Lock base, indexes, fractions and complex formula boundaries involved in manual review feedback item by item."""
    diagnostics = _flash_script_diagnostics(pdf_name)

    for (page_index, source_index), expected in _REVIEWED_SCRIPT_EXPECTATIONS[pdf_name].items():
        script_line = next(line for line in diagnostics[page_index]["script_lines"] if line.source_index == source_index)
        actual = tuple(
            (script_line.text[script_range.start : script_range.end], script_range.style)
            for script_range in script_line.script_ranges
        )
        assert actual == expected, (page_index + 1, source_index, script_line.text)
        materialized = tuple(
            (item["text"], item["role"])
            for item in diagnostics[page_index]["materialized_ranges"]
            if item["source_index"] == source_index
        )
        assert materialized == expected, (page_index + 1, source_index, script_line.text)


@pytest.mark.parametrize("pdf_name", tuple(_RUN_CLOSURE_EXPECTATIONS))
def test_user_reviewed_flash_script_run_closures(pdf_name: str) -> None:
    """Verify that operators, decimal points, and author brackets remain contiguous with co-baseline subscripts run."""
    diagnostics = _flash_script_diagnostics(pdf_name)

    for (page_index, source_index), expected in _RUN_CLOSURE_EXPECTATIONS[pdf_name].items():
        script_line = next(line for line in diagnostics[page_index]["script_lines"] if line.source_index == source_index)
        actual = tuple(
            (script_line.text[script_range.start : script_range.end], script_range.style)
            for script_range in script_line.script_ranges
        )
        materialized = tuple(
            (item["text"], item["role"])
            for item in diagnostics[page_index]["materialized_ranges"]
            if item["source_index"] == source_index
        )
        assert all(item in actual for item in expected), (page_index + 1, source_index, script_line.text)
        assert all(item in materialized for item in expected), (page_index + 1, source_index, script_line.text)


@pytest.mark.parametrize("pdf_name", tuple(_NO_SCRIPT_SOURCE_EXPECTATIONS))
def test_user_reviewed_unicode_math_tokens_stay_body(pdf_name: str) -> None:
    """Verify that standalone Greek without internal base token is not subscripted due to CJK text baseline."""
    diagnostics = _flash_script_diagnostics(pdf_name)

    for page_index, source_index in _NO_SCRIPT_SOURCE_EXPECTATIONS[pdf_name]:
        script_line = next(line for line in diagnostics[page_index]["script_lines"] if line.source_index == source_index)
        assert script_line.script_ranges == ()
        assert not any(item["source_index"] == source_index for item in diagnostics[page_index]["materialized_ranges"])


@pytest.mark.parametrize("pdf_name", tuple(_GENERAL_RECOVERY_EXPECTATIONS))
def test_general_flash_script_recovery_candidates_materialize(pdf_name: str) -> None:
    """Validation footnotes, adjacencies base and numeric references are detected and materialized via common geometric rules."""

    diagnostics = _flash_script_diagnostics(pdf_name)
    for (page_index, source_index), expected in _GENERAL_RECOVERY_EXPECTATIONS[pdf_name].items():
        script_line = next(line for line in diagnostics[page_index]["script_lines"] if line.source_index == source_index)
        actual = tuple((script_line.text[item.start : item.end], item.style) for item in script_line.script_ranges)
        materialized = tuple(
            (item["text"], item["role"])
            for item in diagnostics[page_index]["materialized_ranges"]
            if item["source_index"] == source_index
        )
        assert all(item in actual for item in expected), (page_index + 1, source_index, script_line.text)
        assert all(item in materialized for item in expected), (page_index + 1, source_index, script_line.text)


def test_chinese_paper_page_12_recovers_numbered_formula_regions() -> None:
    """Verify that formulas 6, 7, and 8 at the top of the column are fully claimed as equation and do not absorb adjacent text."""
    pages, diagnostics = _flash_script_analysis("中文论文2.pdf")

    equations = {
        tag: block
        for block in pages[11]
        if block.get("type") == "equation" and isinstance((content := block.get("content")), str)
        for tag in ("6", "7", "8")
        if f"\\tag{{{tag}}}" in content
    }
    assert set(equations) == {"6", "7", "8"}
    assert 0.6 < equations["6"]["bbox"][0] < 0.7
    assert equations["6"]["bbox"][1] < equations["7"]["bbox"][1] < equations["8"]["bbox"][1]
    assert all(block["bbox"][2] > 0.9 for block in equations.values())
    assert all("如下" not in block["content"] and "然后根据" not in block["content"] for block in equations.values())
    assert not any(item["source_index"] in {61, 64} for item in diagnostics[11]["materialized_ranges"])


@pytest.mark.parametrize(
    ("pdf_name", "expected"),
    [
        pytest.param(
            "中文论文.pdf",
            (("2", "superscript"), ("［1］", "superscript"), ("-3", "superscript")),
            id="chinese-paper",
        ),
        pytest.param(
            "中文论文2.pdf",
            (("[1]", "superscript"), ("[82]", "superscript"), ("i", "subscript")),
            id="chinese-paper-2",
        ),
        pytest.param(
            "demo3.pdf",
            (
                ("i", "subscript"),
                ("BASE", "subscript"),
                ("LARGE", "subscript"),
                ("BASE-SAT", "subscript"),
                ("BASE-SO", "subscript"),
            ),
            id="model-subscripts",
        ),
        pytest.param(
            "demo4.pdf",
            (("18", "superscript"), ("2", "superscript")),
            id="isotope-and-unit",
        ),
    ],
)
def test_real_flash_pdfs_materialize_confirmed_scripts(
    pdf_name: str,
    expected: tuple[tuple[str, str], ...],
) -> None:
    """Verification of authentic Flash Confirmed superscripts and subscripts of PDF into final InlineSpan."""
    runs = _flash_script_runs(pdf_name)

    assert all(
        any(content == expected_content and role in styles for content, styles in runs) for expected_content, role in expected
    )


def test_real_flash_plain_layouts_do_not_gain_scripts() -> None:
    """Verify that the rotation and container text of the financial chart sample does not produce Flash subscripts and subscripts."""
    assert _flash_script_runs("caibao1.pdf") == []


def test_zh2_normal_english_words_stay_plain_in_flash() -> None:
    """Verify that the ordinary mixed font English of Chinese Paper 2 will not be mislabeled with superscript or subscript by Flash."""
    styled_text = {content for content, _styles in _flash_script_runs("中文论文2.pdf")}

    assert styled_text.isdisjoint({"Source", "Hypothesis", "Reference", "BLEU", "ROUGE"})
