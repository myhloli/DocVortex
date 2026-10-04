from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

from _flash_pdf_test_utils import (
    formula_detection_evidence,
    _geometry_summary_mismatch,
    _page_bbox_fingerprint,
    _page_fingerprint,
)

from docvortex.analyzers.native.pdf.pipeline import _analyze_native_document
from docvortex.document.pdf._document import PDFDocument

_PROJECT_ROOT = Path(__file__).parents[2]
_EXPECTATION_PATH = _PROJECT_ROOT / "tests" / "fixtures" / "flash_layout_semantic_expectations.json"
_GEOMETRY_MANIFEST_PATH = _PROJECT_ROOT / "tests" / "fixtures" / "flash_layout_geometry_manifest.json"
_BLOCK_EXPECTATION_PATH = _PROJECT_ROOT / "tests" / "fixtures" / "flash_layout_block_expectations.json"
_FROZEN_SOIL_PATH = _PROJECT_ROOT / "demo" / "pdfs" / "中文论文.pdf"
_NATURAL_TEXT_TYPES = {"text", "doc_title", "paragraph_title"}


def _visible_text(value: Any) -> str:
    """Recursively extract the visible text of the real Flash block and InlineSpan."""

    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "".join(_visible_text(item) for item in value)
    if isinstance(value, dict):
        content = value.get("content")
        if isinstance(content, (str, list, dict)):
            return _visible_text(content)
        for field_name in ("text", "latex", "html"):
            field_value = value.get(field_name)
            if isinstance(field_value, str):
                return field_value
    return ""


def _normalized_text(
    value: Any,
    *,
    nfkc: bool = False,
) -> str:
    """Remove typographical whitespace, retaining semantic characters for comparison against versioning expectations."""

    normalized = re.sub(r"\s+", "", _visible_text(value))
    return unicodedata.normalize("NFKC", normalized) if nfkc else normalized


@lru_cache(maxsize=1)
def _expectation() -> dict[str, Any]:
    """Read the semantic expectations of Chinese paper Flash and verify the source file fingerprint."""

    payload = json.loads(_EXPECTATION_PATH.read_text(encoding="utf-8"))
    document = payload["documents"][0]
    source = _PROJECT_ROOT / document["path"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == document["sha256"]
    return document


@lru_cache(maxsize=1)
def _pages() -> tuple[tuple[dict[str, Any], ...], ...]:
    """The real Chinese paper is parsed only once for reuse by all semantic assertions in this document."""

    source = _PROJECT_ROOT / _expectation()["path"]
    with PDFDocument(str(source)) as document, formula_detection_evidence():
        pages = _analyze_native_document(document)
    return tuple(tuple(page) for page in pages)


@lru_cache(maxsize=1)
def _frozen_soil_pages() -> tuple[tuple[dict[str, Any], ...], ...]:
    """The Chinese paper 1 is parsed only once for reuse by the canonical geometric compatibility assertion."""

    with PDFDocument(str(_FROZEN_SOIL_PATH)) as document, formula_detection_evidence():
        pages = _analyze_native_document(document)
    return tuple(tuple(page) for page in pages)


@lru_cache(maxsize=None)
def _additional_chinese_paper_pages(
    relative_path: str,
) -> tuple[tuple[dict[str, Any], ...], ...]:
    """Cache the new Chinese paper Flash page by versioned relative path."""

    source = _PROJECT_ROOT / relative_path
    with PDFDocument(str(source)) as document, formula_detection_evidence():
        pages = _analyze_native_document(document)
    return tuple(tuple(page) for page in pages)


@lru_cache(maxsize=1)
def _block_expectations() -> tuple[dict[str, Any], ...]:
    """Reading versioned block grouping and type inventory expectations of four Chinese papers."""

    payload = json.loads(
        _BLOCK_EXPECTATION_PATH.read_text(encoding="utf-8"),
    )
    assert payload["schema_version"] == 1
    for document in payload["documents"]:
        source = _PROJECT_ROOT / document["path"]
        assert hashlib.sha256(source.read_bytes()).hexdigest() == document["sha256"]
    return tuple(payload["documents"])


def _pages_for_expectation(
    expectation: dict[str, Any],
) -> tuple[tuple[dict[str, Any], ...], ...]:
    """Returns the actual cached Flash page by versioned path."""

    if expectation["path"] == "demo/pdfs/中文论文.pdf":
        return _frozen_soil_pages()
    if expectation["path"] == "demo/pdfs/中文论文2.pdf":
        return _pages()
    return _additional_chinese_paper_pages(
        str(expectation["path"]),
    )


def _block_containing_fragment(
    page: tuple[dict[str, Any], ...],
    fragment: str,
    *,
    block_type: str | None = None,
    nfkc: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Returns the only top-level block containing a normalized fragment and its in-page index."""

    normalized_fragment = _normalized_text(
        fragment,
        nfkc=nfkc,
    )
    matches = [
        (index, block)
        for index, block in enumerate(page)
        if (block_type is None or block.get("type") == block_type)
        and normalized_fragment
        in _normalized_text(
            block.get("content"),
            nfkc=nfkc,
        )
    ]
    assert len(matches) == 1, (
        fragment,
        block_type,
        [
            (
                index,
                block.get("type"),
                _normalized_text(
                    block.get("content"),
                    nfkc=nfkc,
                ),
            )
            for index, block in matches
        ],
    )
    return matches[0]


def test_frozen_soil_paper_keeps_tracked_semantic_and_bbox_gold() -> None:
    """When verifying that there is no long document threshold, Chinese paper 1 still maintains the existing semantics and bbox fingerprint page by page."""

    manifest = json.loads(
        _GEOMETRY_MANIFEST_PATH.read_text(encoding="utf-8"),
    )
    expected = next(document for document in manifest["documents"] if document["path"] == "demo/pdfs/中文论文.pdf")
    assert hashlib.sha256(_FROZEN_SOIL_PATH.read_bytes()).hexdigest() == expected["sha256"]
    pages = _frozen_soil_pages()
    assert [_page_fingerprint(list(page)) for page in pages] == [page["fingerprint"] for page in expected["pages"]]
    assert [_page_bbox_fingerprint(list(page)) for page in pages] == [page["bbox_fingerprint"] for page in expected["pages"]]


def _typed_texts(block_type: str) -> Counter[tuple[int, str]]:
    """Specify types by page number and specification text statistics, retaining the ability to detect duplicates."""

    return Counter(
        (page_index, _normalized_text(block.get("content")))
        for page_index, page in enumerate(_pages())
        for block in page
        if block.get("type") == block_type
    )


def test_chinese_paper_matches_versioned_title_semantics() -> None:
    """Verify that the bilingual document title and forty chapter titles are fully consistent with the artificial visual gold mark."""

    expectation = _expectation()
    expected_doc_titles = Counter((item["page_index"], item["text"]) for item in expectation["doc_titles"])
    expected_paragraph_titles = Counter((item["page_index"], item["text"]) for item in expectation["paragraph_titles"])

    assert _typed_texts("doc_title") == expected_doc_titles
    assert _typed_texts("paragraph_title") == expected_paragraph_titles
    title_text = "\n".join(text for _page_index, text in _typed_texts("paragraph_title"))
    assert all(fragment not in title_text for fragment in expectation["forbidden_title_fragments"])


def test_chinese_paper_recovers_all_numbered_formula_blocks() -> None:
    """Verify that Equations 1 to 14 are uniquely block-like, and the content of the formulas does not absorb adjacent explanatory sentences."""

    expectation = _expectation()
    equations = [block for page in _pages() for block in page if block.get("type") == "equation"]
    tag_counts: Counter[str] = Counter()
    contents = []
    for block in equations:
        content = _normalized_text(block.get("content"))
        contents.append(content)
        match = re.search(r"\\tag\{([^}]+)\}", content)
        assert match is not None
        tag_counts[match.group(1)] += 1

    assert tag_counts == Counter(expectation["equation_tags"])
    assert all(fragment not in content for content in contents for fragment in expectation["forbidden_equation_fragments"])


def test_chinese_paper_preserves_visual_controls_and_unique_blocks() -> None:
    """Verify that table and figure page numbers, double-column attributions, and top-level natural text do not overlap nearly completely."""

    expectation = _expectation()
    counts = Counter(str(block.get("type")) for page in _pages() for block in page)
    assert all(counts[block_type] == expected_count for block_type, expected_count in expectation["control_counts"].items())

    first_page = _pages()[0]
    introduction = next(
        block
        for block in first_page
        if block.get("type") == "paragraph_title" and _normalized_text(block.get("content")) == "0引言"
    )
    introduction_bottom = float(introduction["bbox"][3])
    assert not any(
        block.get("type") == "text"
        and float(block["bbox"][1]) >= introduction_bottom
        and float(block["bbox"][1]) < 0.84
        and float(block["bbox"][0]) < 0.48
        and float(block["bbox"][2]) > 0.52
        for block in first_page
    )

    overlaps = []
    for page_index, page in enumerate(_pages()):
        blocks = [block for block in page if block.get("type") in _NATURAL_TEXT_TYPES and isinstance(block.get("bbox"), list)]
        for first_index, first in enumerate(blocks):
            first_bbox = [float(value) for value in first["bbox"]]
            first_area = max(0.0, first_bbox[2] - first_bbox[0]) * max(
                0.0,
                first_bbox[3] - first_bbox[1],
            )
            for second in blocks[first_index + 1 :]:
                second_bbox = [float(value) for value in second["bbox"]]
                second_area = max(0.0, second_bbox[2] - second_bbox[0]) * max(
                    0.0,
                    second_bbox[3] - second_bbox[1],
                )
                intersection = max(
                    0.0,
                    min(first_bbox[2], second_bbox[2]) - max(first_bbox[0], second_bbox[0]),
                ) * max(
                    0.0,
                    min(first_bbox[3], second_bbox[3]) - max(first_bbox[1], second_bbox[1]),
                )
                overlap = intersection / max(
                    1e-9,
                    min(first_area, second_area),
                )
                if overlap >= 0.95:
                    overlaps.append(
                        (
                            page_index,
                            _normalized_text(first.get("content")),
                            _normalized_text(second.get("content")),
                        )
                    )
    assert overlaps == []


def test_chinese_papers_match_versioned_block_group_expectations() -> None:
    """Verify that the type inventory, specified merging, and specified splitting of four real papers are consistent with human review expectations."""

    for expectation in _block_expectations():
        pages = _pages_for_expectation(expectation)
        normalize_nfkc = expectation.get("normalize_nfkc") is True
        counts = Counter(str(block.get("type")) for page in pages for block in page)
        assert counts == Counter(expectation["type_counts"])

        for item in expectation.get("exact_typed_text", []):
            page = pages[item["page_index"]]
            matches = [
                block
                for block in page
                if block.get("type") == item["type"]
                and _normalized_text(
                    block.get("content"),
                    nfkc=normalize_nfkc,
                )
                == _normalized_text(
                    item["text"],
                    nfkc=normalize_nfkc,
                )
            ]
            assert len(matches) == 1, item

        for group in expectation.get("same_block_groups", []):
            page = pages[group["page_index"]]
            matched_indices = {
                _block_containing_fragment(
                    page,
                    fragment,
                    block_type=group["type"],
                    nfkc=normalize_nfkc,
                )[0]
                for fragment in group["fragments"]
            }
            assert len(matched_indices) == 1, group

        for group in expectation.get("different_block_groups", []):
            page = pages[group["page_index"]]
            matched_indices = {
                _block_containing_fragment(
                    page,
                    fragment,
                    nfkc=normalize_nfkc,
                )[0]
                for fragment in group["fragments"]
            }
            assert len(matched_indices) == len(group["fragments"]), group

        for item in expectation.get("forbidden_type_fragments", []):
            page = pages[item["page_index"]]
            normalized_fragment = _normalized_text(
                item["fragment"],
                nfkc=normalize_nfkc,
            )
            assert not any(
                block.get("type") == item["type"]
                and normalized_fragment
                in _normalized_text(
                    block.get("content"),
                    nfkc=normalize_nfkc,
                )
                for block in page
            ), item


def test_chinese_paper_four_third_page_upper_band_inventory() -> None:
    """Verify that the original fake large table area on the third page of Chinese paper 4 is restored to the specified text, chart and title inventory."""

    pages = _additional_chinese_paper_pages(
        "demo/pdfs/中文论文4.pdf",
    )
    upper_band = [
        block
        for block in pages[2]
        if block.get("type") != "header" and isinstance(block.get("bbox"), list) and float(block["bbox"][3]) <= 0.56
    ]

    counts = Counter(str(block.get("type")) for block in upper_band)
    assert counts == Counter({"paragraph_title": 3, "text": 4, "image": 1, "caption": 2, "table": 1})


def test_chinese_paper_continuation_caption_precedes_tight_table_body() -> None:
    """Verify that table caption, continued on page 3, does not enter HTML and that its boundary is above the tightened table body."""

    page = _pages()[2]
    _caption_index, caption = _block_containing_fragment(
        page,
        "续表",
        block_type="caption",
    )
    table = next(block for block in page if block.get("type") == "table")

    assert _normalized_text(caption.get("content")) == "续表"
    assert "续表" not in str(table.get("content") or "")
    assert float(caption["bbox"][3]) < float(table["bbox"][1])


def test_flash_layout_geometry_summary_comparison_is_strict() -> None:
    """Missing verification geometry summaries or drifting of either count will result in independent gating failure."""

    expected = {
        "expected_geometry_summary": {
            "repaired_chars": 10,
            "repaired_lines": 2,
        }
    }
    actual = {
        "geometry_summary": {
            "repaired_chars": 10,
            "repaired_lines": 2,
        }
    }

    assert _geometry_summary_mismatch("sample.pdf", expected, actual) is None
    assert _geometry_summary_mismatch("sample.pdf", {}, actual) == {
        "file": "sample.pdf",
        "reason": "geometry_summary_expectation_missing",
    }
    assert _geometry_summary_mismatch(
        "sample.pdf",
        expected,
        {
            "geometry_summary": {
                "repaired_chars": 10,
                "repaired_lines": 1,
            }
        },
    ) == {
        "file": "sample.pdf",
        "reason": "geometry_summary_mismatch",
        "expected": expected["expected_geometry_summary"],
        "actual": {
            "repaired_chars": 10,
            "repaired_lines": 1,
        },
    }
