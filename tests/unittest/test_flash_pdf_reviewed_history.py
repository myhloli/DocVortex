"""锁定 cefa1208 重放并逐页审阅后的历史输出；旧几何清单的陈旧条目不整体覆盖。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

from _flash_pdf_test_utils import _assert_history_page, _visible_text, formula_detection_evidence
from test_flash_pdf_char_geometry import _read_pdf_fixture
from docvortex.analyzers.native.pdf.pipeline import _analyze_native_document
from docvortex.document.pdf import PDFDocument

ROOT = Path(__file__).parents[2]
MANIFEST = json.loads((ROOT / "tests/fixtures/flash_reviewed_history.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("document", MANIFEST["documents"], ids=lambda document: document["name"])
def test_reviewed_historical_pages_keep_content_order_types_and_bounds(document: dict) -> None:
    """覆盖 19 份 168 页的逐页类型、内容、阅读顺序和边界，公式文本使用既有检测证据辅助器。"""
    payload = _read_pdf_fixture(ROOT / document["path"])
    assert hashlib.sha256(payload).hexdigest() == document["sha256"]
    with PDFDocument(payload) as pdf, formula_detection_evidence():
        pages = _analyze_native_document(pdf)
    assert len(pages) == len(document["pages"])
    for index, (page, expected) in enumerate(zip(pages, document["pages"], strict=True)):
        _assert_history_page(page, expected, (document["name"], index + 1), sys.platform)


def test_mixed_elements_ligature_words_match_visible_source() -> None:
    """整本五页原件的标题和页眉不拆开连字词，避免金标保留旧的字形内部空格。"""
    document = next(item for item in MANIFEST["documents"] if item["name"] == "mixed_elements_pages_11_15")
    payload = _read_pdf_fixture(ROOT / document["path"])
    assert hashlib.sha256(payload).hexdigest() == document["sha256"]
    with PDFDocument(payload) as pdf:
        pages = _analyze_native_document(pdf)
    assert len(pages) == 5
    for index, number in ((0, 2), (1, 1)):
        expected = f"{number}. Effects on esophageal lesions induced by reflux of gastric juice"
        assert sum(_visible_text(block) == expected for block in pages[index]) == 1
    for index in (2, 4):
        assert sum(block["type"] == "header" and _visible_text(block) == "Inflammopharmacology" for block in pages[index]) == 1


def test_demo2_figure_keeps_native_minus_glyph() -> None:
    """图中视差公式的减号由 /minus 恢复，不能为匹配旧缺字金标而再次丢弃。"""
    document = next(item for item in MANIFEST["documents"] if item["name"] == "demo2")
    with PDFDocument(_read_pdf_fixture(ROOT / document["path"])) as pdf:
        pages = _analyze_native_document(pdf)
    figures = [block for block in pages[0] if block["type"] == "image"]
    assert sum("dp =¯x − x" in _visible_text(block) for block in figures) == 1
