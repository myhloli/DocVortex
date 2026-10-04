"""Arrange original layout's inline formulas independently between frozen body text and headings."""

from __future__ import annotations

from statistics import median

from ....content.inline import inline_plain_text
from ....schema import RefTextBlock, TextBlock
from .diagnostics import report_pdf_diagnostic
from .font_plan import BlockFit, PreparedBlock
from .formula import DisplayFormulaFlowable

Rect = tuple[float, float, float, float]
_GAP = 2.0
_EPS = 0.001


def display_formula(item: PreparedBlock) -> DisplayFormulaFlowable | None:
    """Only select independent vector interline formulas, and continue to use the original block adaptation for pictures and text."""
    if len(item.flowables) == 1 and isinstance(item.flowables[0], DisplayFormulaFlowable):
        return item.flowables[0]
    return None


def _body_candidates(blocks: list[PreparedBlock]) -> list[PreparedBlock]:
    """Use a long enough real text to estimate the font size, and exclude short link segments and placeholder content such as and."""
    return [
        item
        for item in blocks
        if item.body_font_size is not None
        and isinstance(item.block, (TextBlock, RefTextBlock))
        and len("".join(inline_plain_text(item.block.content).split())) >= 20
    ]


def _same_column(item: PreparedBlock, body: PreparedBlock) -> bool:
    """By assigning the original horizontal coverage to the main text column, avoid mixing the font sizes and widths of the opposite side columns."""
    x0, _, x1, _ = item.original_rect
    bx0, _, bx1, _ = body.original_rect
    overlap = min(x1, bx1) - max(x0, bx0)
    return bx0 - _EPS <= (x0 + x1) / 2 <= bx1 + _EPS and overlap >= 0.5 * min(x1 - x0, bx1 - bx0)


def _safe_area(item: PreparedBlock, blocks: list[PreparedBlock], left: float, right: float) -> Rect | None:
    """The final drawing range of other blocks is used as a barrier; unarranged formulas are reserved for the original box, and white space is allocated in reading order."""
    _, y0, _, y1 = item.original_rect
    top, bottom = _GAP, item.page_height - _GAP
    for other in blocks:
        if other is item:
            continue
        bx0, by0, bx1, by1 = other.draw_rect or other.original_rect
        if min(right, bx1) <= max(left, bx0) - _GAP + _EPS:
            continue
        if by1 <= y0 + _EPS:
            top = max(top, by1 + _GAP)
        elif by0 >= y1 - _EPS:
            bottom = min(bottom, by0 - _GAP)
        else:
            return None
    return (left, top, right, bottom) if bottom > top and right > left else None


def place_formulas(blocks: list[PreparedBlock], page_width: float, default_font_size: float) -> None:
    """Arrange all formulas in the same column safe area; font size and horizontal positioning no longer scale with the entire canvas."""
    bodies = _body_candidates(blocks)
    page_font_size = median(body.body_font_size for body in bodies) if bodies else default_font_size
    for item in blocks:
        flowable = display_formula(item)
        if flowable is None:
            continue
        column = [body for body in bodies if _same_column(item, body)]
        font_size = median(body.body_font_size for body in column) if column else page_font_size
        x0, y0, x1, y1 = item.original_rect
        left = max(0.0, median(body.original_rect[0] for body in column)) if column else x0
        right = min(page_width, median(body.original_rect[2] for body in column)) if column else x1
        area = _safe_area(item, blocks, left, right) or _safe_area(item, blocks, x0, x1)
        if area is None:
            # The original box will not be expanded when the inputs already intersect or there is no safe distance, and a clear diagnosis will be provided to the caller.
            area = item.original_rect
            report_pdf_diagnostic(
                "pdf_formula_clearance_unavailable",
                f"Using original formula box; {flowable.location}",
                item.page_idx,
            )
        left, top, right, bottom = area
        flowable.fit_to_box(right - left, font_size=font_size, max_height=bottom - top)
        draw_top = min(max((y0 + y1 - flowable.height) / 2, top), bottom - flowable.height)
        item.reference_font_size = item.target_font_size = font_size
        item.draw_rect = (left, draw_top, right, draw_top + flowable.height)
        # Flowable completes the body scaling internally, the external canvas ratio is always 1, and the serial number will not be shrunk to the left.
        item.fit = BlockFit(1.0, flowable.width, flowable.height, [(flowable, flowable.width, flowable.height, 0.0)])
        report_pdf_diagnostic(
            "pdf_formula_layout",
            f"target_font_size={font_size:.4f}, formula_font_size={flowable.effective_font_size:.4f}, "
            f"tag_font_size={flowable.effective_tag_font_size}, original_bbox_pt={item.original_rect}, "
            f"draw_bbox_pt={item.draw_rect}; {flowable.location}",
            item.page_idx,
        )
