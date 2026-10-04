"""The real PDF input is maintained by the engine; the host Hybrid only consumes frozen character snapshots."""

import hashlib
from pathlib import Path

import pytest

from docvortex.document.pdf import PDFDocument
from docvortex.document.pdf._document import get_lines_from_chars

_PDF_ROOT = Path(__file__).parent / "pdfs"
_SOURCE_HASHES = {
    "native_cjk_layout_synthetic.pdf": "78ae9e8f670a99c17058ec53e5abdf7631671830e84461803ed2f00c1778ee59",
    "metabolic_pathway_page_3.pdf": "f383399e3459b20e35a13290a1e813dcd964850c3bf945fe64fb1cd3bfe54b12",
}


def _source_lines(filename: str, page_index: int) -> tuple[int, list[dict]]:
    """Returns real page rotation and physical rows after verifying versioned PDF identity to avoid verifying only the host snapshot."""
    source = _PDF_ROOT / filename
    assert hashlib.sha256(source.read_bytes()).hexdigest() == _SOURCE_HASHES[filename]
    with PDFDocument(source.read_bytes()) as document:
        geometry = document.get_page_chars_with_geometry(page_index)
        return document[page_index].rotation, get_lines_from_chars(geometry.chars)


@pytest.mark.parametrize(
    ("probe", "minimum_fonts"),
    [
        ("• 合成支持工程师 Support Engineer", 2),
        ("• 混合字体工具 Acuity Toolkit", 2),
        ("• 测试系统 Ubuntu 24.04", 2),
        ("mineru_toolkit_binary_1.0.0_linux_x86_64.tgz", 1),
    ],
)
def test_synthetic_cjk_source_preserves_mixed_font_probes(probe: str, minimum_fonts: int) -> None:
    """The four original probes must still be extractable from the real PDF with original mixed font evidence retained."""
    _, lines = _source_lines("native_cjk_layout_synthetic.pdf", 2)
    matches = [line for line in lines if probe in "".join(span["text"] for span in line["spans"])]
    assert len(matches) == 1
    fonts = {
        str((char.get("font") or {}).get("name", "")) for line in matches for span in line["spans"] for char in span["chars"]
    }
    assert len(fonts) >= minimum_fonts


def test_rotated_powerpoint_source_preserves_page_and_physical_lines() -> None:
    """A real rotated page still has the same rotation, title, line count, and specified font, and host snapshots cannot mask extraction degradation."""
    rotation, lines = _source_lines("metabolic_pathway_page_3.pdf", 0)
    assert rotation == 90
    assert len(lines) == 33
    assert "".join(span["text"] for span in lines[0]["spans"]) == "Energy Metabolism"
    assert any(
        (char.get("font") or {}).get("name") == "CalifornianFB-Reg"
        for line in lines
        for span in line["spans"]
        for char in span["chars"]
    )
