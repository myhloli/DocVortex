"""Conversion regression testing for non-standard OOXML packages (non-conventional part paths, ISO Strict, missing sldSz, AlternateContent formulas)."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

from docvortex.analyzers.native import DocxModel, PptxModel
from docvortex.schema import BlockType

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _block_text(block: dict) -> str:
    """Compress content (string or arbitrarily deeply nested inline span) of block to plain text."""
    content = block.get("content")

    def render(value) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return " ".join(filter(None, (render(part) for part in value)))
        if isinstance(value, dict):
            return render(value.get("content"))
        return ""

    return render(content)


def _flatten_pages(pages: list[list[dict]]) -> list[dict]:
    """Expand the paginated block list and filter for empty content."""
    return [block for page in pages for block in page]


def _convert(model, fixture: Path) -> str:
    """Converts fixture and returns the entire text content for substring assertion."""
    pages = model().predict(BytesIO(fixture.read_bytes()))
    blocks = _flatten_pages(pages)
    return "\n".join(filter(None, (_block_text(block) for block in blocks)))


ROBUST_CASES = [
    (
        "handmade-altpath.docx",
        DocxModel,
        ["Relocated heading", "Body found via rels"],
    ),
    (
        "handmade-strict.docx",
        DocxModel,
        ["Strict heading", "toggled bold"],
    ),
    (
        "handmade-altpath.pptx",
        PptxModel,
        ["Relocated deck title"],
    ),
    (
        "handmade-strict.pptx",
        PptxModel,
        ["Strict slide title", "Strict body text"],
    ),
    (
        "handmade-order.pptx",
        PptxModel,
        ["Kicker before the title", "Title placed second", "Body after the title"],
    ),
    (
        "handmade-inherit.pptx",
        PptxModel,
        ["Inherited Title Slide", "Roman three bold via master", "Plain closing line"],
    ),
    (
        "handmade-links.pptx",
        PptxModel,
        ["Jump to the second slide", "External link", "Second slide content"],
    ),
    (
        "handmade-math.pptx",
        PptxModel,
        ["Quadratic formula", "Solve with"],
    ),
]


@pytest.mark.parametrize(
    ("filename", "model", "expected_fragments"),
    ROBUST_CASES,
    ids=[case[0] for case in ROBUST_CASES],
)
def test_robust_ooxml_fixture_converts(model, filename: str, expected_fragments: list[str]) -> None:
    """Non-standard packages should be fully converted and retain critical content, rather than failing at the entry or parsing stage."""
    text = _convert(model, FIXTURES / ("docx" if filename.endswith(".docx") else "pptx") / filename)
    for fragment in expected_fragments:
        assert fragment in text, f"{fragment!r} missing from {filename} output:\n{text}"


def test_handmade_math_pptx_outputs_alternate_content_formula() -> None:
    """AlternateContent OMML formulas in the Choice branch should be converted to LaTeX output."""
    pages = PptxModel().predict(BytesIO((FIXTURES / "pptx" / "handmade-math.pptx").read_bytes()))
    equation_text = "\n".join(
        _block_text(block)
        for block in _flatten_pages(pages)
        if block.get("type") in {BlockType.EQUATION, BlockType.TEXT}
    )
    assert "frac" in equation_text
    assert "2a" in equation_text
