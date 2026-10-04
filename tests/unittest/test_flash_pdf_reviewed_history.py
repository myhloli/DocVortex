"""Lock cefa1208 Historical output after replay and page-by-page review; stale entries from old geometry lists are not overwritten in their entirety."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

from _flash_pdf_test_utils import _assert_history_page, formula_detection_evidence
from test_flash_pdf_char_geometry import _read_pdf_fixture
from docvortex.analyzers.native.pdf.pipeline import _analyze_native_document
from docvortex.document.pdf import PDFDocument

ROOT = Path(__file__).parents[2]
MANIFEST = json.loads((ROOT / "tests/fixtures/flash_reviewed_history.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("document", MANIFEST["documents"], ids=lambda document: document["name"])
def test_reviewed_historical_pages_keep_content_order_types_and_bounds(document: dict) -> None:
    """Covering page-by-page type, content, reading order and boundaries in 19 copies of 168 pages, Formula Text uses established detection evidence aids."""
    payload = _read_pdf_fixture(ROOT / document["path"])
    assert hashlib.sha256(payload).hexdigest() == document["sha256"]
    with PDFDocument(payload) as pdf, formula_detection_evidence():
        pages = _analyze_native_document(pdf)
    assert len(pages) == len(document["pages"])
    for index, (page, expected) in enumerate(zip(pages, document["pages"], strict=True)):
        _assert_history_page(page, expected, (document["name"], index + 1), sys.platform)
