"""Verify that PDF rendering diagnostics use DEBUG uniformly and the structured results remain unchanged."""

from __future__ import annotations

import pytest
from loguru import logger

from docvortex.render._internal.pdf.diagnostics import collect_pdf_diagnostics, report_pdf_diagnostic
from docvortex.result import Diagnostic


_PDF_RENDER_DIAGNOSTIC_CODES = (
    "pdf_layout_font_exception",
    "pdf_layout_small_text",
    "pdf_layout_scaled",
    "pdf_title_geometry_conflict",
    "pdf_title_clearance_unavailable",
    "pdf_title_layout_expanded",
    "pdf_layout_approximate",
    "pdf_layout_visual_fallback",
    "pdf_layout_reflow_fallback",
    "pdf_formula_fallback",
    "pdf_table_fallback",
    "pdf_image_unavailable",
    "pdf_content_placeholder",
    "pdf_duplicate_anchor",
    "pdf_unmatched_link",
    "unknown_diagnostic",
)


@pytest.mark.parametrize("code", _PDF_RENDER_DIAGNOSTIC_CODES)
def test_all_pdf_render_diagnostics_use_debug(code: str) -> None:
    """All PDF rendering diagnostics are DEBUG, and duplicate logs do not change the structured diagnostic deduplication results."""
    records: list[str] = []
    warnings: list[str] = []
    debug_sink = logger.add(records.append, level="DEBUG", format="{level.name}|{message}")
    warning_sink = logger.add(warnings.append, level="WARNING", format="{level.name}|{message}")
    try:
        with collect_pdf_diagnostics() as items:
            report_pdf_diagnostic(code, "example", 4)
            report_pdf_diagnostic(code, "example", 4)
        assert items == [Diagnostic(code, "example", 4)]
        assert [str(message).strip() for message in records] == [f"DEBUG|{code}: example"] * 2
        assert warnings == []
    finally:
        logger.remove(debug_sink)
        logger.remove(warning_sink)
