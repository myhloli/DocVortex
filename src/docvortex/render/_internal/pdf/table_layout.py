"""Region adaptation, rotation and safe whitespace allocation of original layout structure tables."""

from __future__ import annotations

from copy import deepcopy
from collections import OrderedDict
from dataclasses import dataclass
from math import isfinite
from statistics import median
from typing import Callable

from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Flowable, Table

from ....schema import PAGE_AUXILIARY_BLOCK_TYPES
from .diagnostics import report_pdf_diagnostic
from .font_plan import BlockFit, PreparedBlock
from .formula_layout import _body_candidates, _safe_area, _same_column
from .table import PdfTableError

Rect = tuple[float, float, float, float]


@dataclass(frozen=True)
class _TableMeasurement:
    """Holds the actual tables and scalars exclusive to a trial layout, and does not copy the row layout that has been measured."""

    tables: list[Table]
    heights: list[float]
    font_size: float
    scale: float
    width_ratio: float


@dataclass(frozen=True, slots=True)
class _TrialSummary:
    """Only immutable geometry is cached, not mutable Table, Paragraph or canvas."""

    heights: tuple[float, ...]
    width_ratio: float


class SpatialTableContent(Flowable):
    """A construction callback that saves the structure table and all layouts are rematerialized from the target logical width."""

    def __init__(
        self,
        build: Callable[[float, float], list[Table]],
        fallback: Callable[[float, float, str], Flowable],
        validate: Callable[[Flowable], None],
        *,
        angle: int,
        location: str,
        page_idx: int,
        trial_key: Callable[[], tuple | None] | None = None,
        minimum_height: Callable[[float], float | None] | None = None,
    ) -> None:
        """Binds the context of this table to the materialized material source, without reading the source PDF or modifying the semantic tree."""
        super().__init__()
        self.build = build
        self.fallback = fallback
        self.validate = validate
        self.angle = angle
        self.location = location
        self.page_idx = page_idx
        self.font_size = 8.5
        self.scale = 1.0
        self.width_ratio = 1.0
        self.tables: list[Table] = []
        self.heights: list[float] = []
        self.failure: str | None = None
        self.fallback_flow: Flowable | None = None
        self.width = self.height = 0.0
        self.trial_key = trial_key
        self.minimum_height = minimum_height
        self._measurement_context: tuple | None = None
        self._trial_cache: OrderedDict[tuple, _TrialSummary] = OrderedDict()

    def fork(self) -> SpatialTableContent:
        """Create an independent layout state for another candidate area, and only reuse the read-only content to construct the callback."""
        candidate = SpatialTableContent(
            self.build,
            self.fallback,
            self.validate,
            angle=self.angle,
            location=self.location,
            page_idx=self.page_idx,
            trial_key=self.trial_key,
            minimum_height=self.minimum_height,
        )
        # Confirmed structural or drawing errors continue to use the existing block-wise fallback contract.
        candidate.failure = self.failure
        candidate._trial_cache = self._trial_cache
        return candidate

    def clear_trials(self) -> None:
        """After the page drawing is completed, the geometric summary shared by each area of the same table is released."""
        self._trial_cache.clear()
        self._measurement_context = None

    def _snapshot(self) -> _TableMeasurement:
        """The current successful measurement results are retained, and subsequent test runs will reconstruct your own Table."""
        return _TableMeasurement(self.tables, self.heights, self.font_size, self.scale, self.width_ratio)

    def _restore(self, measurement: _TableMeasurement) -> float:
        """Directly restore the best candidate, keeping the original numerical calculation order without re-constructing or measuring."""
        self.tables, self.heights = measurement.tables, measurement.heights
        self.font_size, self.scale, self.width_ratio = measurement.font_size, measurement.scale, measurement.width_ratio
        return (sum(self.heights) + 2 * (len(self.heights) - 1)) * self.scale

    @property
    def effective_font_size(self) -> float:
        """Returns the base font size of the table after final scaling in the current area."""
        return self.font_size * self.scale

    def _measure(self, width: float, font_size: float, scale: float = 1.0, *, materialize: bool = False) -> float:
        """Column widths and row heights are recalculated; even below 6 pt, the scaled table width still covers the target width."""
        key = (self._measurement_context, width, font_size, scale) if self._measurement_context is not None else None
        cached = self._trial_cache.get(key) if key is not None and not materialize else None
        if cached is not None:
            self._trial_cache.move_to_end(key)
            self.tables = []
            self.heights = list(cached.heights)
            self.width_ratio = cached.width_ratio
            self.font_size, self.scale = font_size, scale
            return (sum(self.heights) + 2 * (len(self.heights) - 1)) * scale
        self.tables = self.build(width / scale, font_size)
        if not self.tables:
            raise PdfTableError("HTML contains no materializable table")
        measurements = [table.wrap(width / scale, 1e9) for table in self.tables]
        if any(not isfinite(w + h) or w <= 0 or h <= 0 for w, h in measurements):
            raise PdfTableError("Invalid structured table dimensions")
        self.width_ratio = max(w * scale / width for w, _ in measurements)
        self.heights = [height for _, height in measurements]
        self.font_size, self.scale = font_size, scale
        if key is not None:
            if len(self._trial_cache) >= 128 and key not in self._trial_cache:
                self._trial_cache.popitem(last=False)
            self._trial_cache[key] = _TrialSummary(tuple(self.heights), self.width_ratio)
            self._trial_cache.move_to_end(key)
        return (sum(self.heights) + 2 * (len(self.heights) - 1)) * scale

    def fit(self, width: float, height: float) -> None:
        """First find the maximum available font size, and then scale based on 6 pt; only roll back the material if typesetting or drawing fails."""
        width, height = max(0.001, width), max(0.001, height)
        if self.failure is not None:
            self._fit_fallback(width, height)
            return
        logical_width, logical_height = (height, width) if self.angle in (90, 270) else (width, height)
        try:
            self._measurement_context = self.trial_key() if self.trial_key is not None else None
            # There are only 26 candidates, and the step-by-step measurement can avoid missing the maximum font size when the height is non-monotonic due to column width redistribution.
            for size in range(85, 59, -1):
                lower_bound = self.minimum_height(size / 10) if size > 60 and self.minimum_height is not None else None
                # Allow extra margin for floating point lower bound; 6 pt must be measured completely to preserve the original initial interval of the scaled search.
                if lower_bound is not None and lower_bound > logical_height + 0.001 + 1e-8:
                    continue
                content_height = self._measure(logical_width, size / 10)
                if content_height <= logical_height + 0.001 and self.width_ratio <= 1 + 1e-8:
                    break
            else:
                high, low = 1.0, min(0.5, logical_height / content_height, 1 / self.width_ratio)
                while self._measure(logical_width, 6.0, low) > logical_height or self.width_ratio > 1 + 1e-8:
                    low /= 2
                best = self._snapshot()
                for _ in range(18):
                    candidate = (low + high) / 2
                    if self._measure(logical_width, 6.0, candidate) <= logical_height and self.width_ratio <= 1 + 1e-8:
                        low = candidate
                        best = self._snapshot()
                    else:
                        high = candidate
                content_height = self._restore(best)
            if not self.tables:
                # Summary hits do not borrow mutable objects from another region; the selected font size still needs to be physically materialized and pre-rendered.
                content_height = self._measure(logical_width, self.font_size, self.scale, materialize=True)
            self.width, self.height = (
                (content_height, logical_width) if self.angle in (90, 270) else (logical_width, content_height)
            )
            self.validate(self)
        except Exception as exc:
            self.failure = f"{type(exc).__name__}: {str(exc)[:200]}"
            self._fit_fallback(width, height)

    def _fit_fallback(self, width: float, height: float) -> None:
        """Use the block template provided by the caller and fully fit the text template into the area."""
        self.fallback_flow = self.fallback(width, height, self.failure or "unavailable HTML")
        measured_width, measured_height = self.fallback_flow.wrap(width, 1e9)
        self.scale = min(1.0, width / max(measured_width, 0.001), height / max(measured_height, 0.001))
        self.width, self.height = measured_width * self.scale, measured_height * self.scale

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:
        """Returns the last regional adaptation result and does not repeat the trial arrangement during the drawing phase."""
        return self.width, self.height

    def draw(self) -> None:
        """After restoring the source direction, the original table is drawn, and the bottom of the picture already contains the direction transformation."""
        self.canv.saveState()
        try:
            if self.fallback_flow is not None:
                self.canv.scale(self.scale, self.scale)
                self.fallback_flow.drawOn(self.canv, 0, 0)
                return
            if self.angle == 90:
                self.canv.translate(0, self.height)
                self.canv.rotate(-90)
            elif self.angle == 270:
                self.canv.translate(self.width, 0)
                self.canv.rotate(90)
            elif self.angle == 180:
                self.canv.translate(self.width, self.height)
                self.canv.rotate(180)
            self.canv.scale(self.scale, self.scale)
            cursor = sum(self.heights) + 2 * (len(self.heights) - 1)
            for table, height in zip(self.tables, self.heights):
                cursor -= height
                table.drawOn(self.canv, 0, cursor)
                cursor -= 2
        finally:
            self.canv.restoreState()


def has_spatial_table(item: PreparedBlock) -> bool:
    """The parent box combination of the table is included when identifying independent tables and missing child boxes."""
    return any(isinstance(flow, SpatialTableContent) for flow in item.flowables)


def _fit_group(item: PreparedBlock, area: Rect, canvas: Canvas) -> BlockFit:
    """Reserve space for table titles and notes; when the parent frame is extremely small, scale the combination synchronously and re-measure according to the compensation width."""
    # Candidates have independent paragraph, picture and table status, and there is no need to cancel the trial arrangement in another area when the original frame wins.
    item.flowables = [flow.fork() if isinstance(flow, SpatialTableContent) else deepcopy(flow) for flow in item.flowables]
    width, height = area[2] - area[0], area[3] - area[1]
    scale = 1.0
    for _ in range(80):
        measured: list[tuple[Flowable, float, float, float]] = []
        fixed_height = previous_after = 0.0
        for index, flow in enumerate(item.flowables):
            gap = max(previous_after, flow.getSpaceBefore()) if index else 0.0
            w, h = (0.0, 0.0) if isinstance(flow, SpatialTableContent) else flow.wrapOn(canvas, width / scale, 1e9)
            measured.append((flow, w, h, gap))
            fixed_height += h + gap
            previous_after = flow.getSpaceAfter()
        if fixed_height * scale < height * 0.75:
            break
        scale *= 0.8
    tables = [flow for flow in item.flowables if isinstance(flow, SpatialTableContent)]
    allocation = max(0.001, height / scale - fixed_height) / len(tables)
    measurements = []
    for flow, w, h, gap in measured:
        if isinstance(flow, SpatialTableContent):
            flow.fit(width / scale, allocation)
            w, h = flow.width, flow.height
        measurements.append((flow, w, h, gap))
    return BlockFit(
        scale,
        max(w for _, w, _, _ in measurements) * scale,
        sum(h + gap for _, _, h, gap in measurements) * scale,
        measurements,
    )


def _quality(item: PreparedBlock, fit: BlockFit) -> float:
    """Compare the candidate areas with the smallest table font size in the combination, and fall back on failure without participating in the readable font size competition."""
    return min(
        flow.effective_font_size * fit.scale if flow.failure is None else 8.5
        for flow in item.flowables
        if isinstance(flow, SpatialTableContent)
    )


def place_tables(blocks: list[PreparedBlock], page_width: float, canvas: Canvas) -> None:
    """Arrange the table after freezing the text, titles, and formulas, giving priority to the original frames, and borrowing safe white space in the same column only when necessary."""
    bodies = _body_candidates(blocks)
    content_rects = [item.original_rect for item in blocks if item.block.type not in PAGE_AUXILIARY_BLOCK_TYPES]
    content_top = min((rect[1] for rect in content_rects), default=0.0)
    content_bottom = max((rect[3] for rect in content_rects), default=0.0)
    for item in blocks:
        if not has_spatial_table(item):
            continue
        original = item.original_rect
        best_area = original
        best_fit = _fit_group(item, original, canvas)
        best_quality = _quality(item, best_fit)
        if best_quality < 8.5 - 0.001:
            column = [body for body in bodies if _same_column(item, body)]
            left = min(original[0], median(body.original_rect[0] for body in column)) if column else original[0]
            right = max(original[2], median(body.original_rect[2] for body in column)) if column else original[2]
            safe = _safe_area(item, blocks, max(0.0, left), min(page_width, right))
            if safe is not None:
                # The safe margin does not include the original page margins, especially to avoid rotating the table and pulling the entire column to the edge of the paper.
                safe = (safe[0], max(safe[1], content_top), safe[2], min(safe[3], content_bottom))
                if safe[3] <= safe[1]:
                    safe = None
            if safe is not None and safe != original:
                trial = _fit_group(item, safe, canvas)
                if _quality(item, trial) > best_quality + 0.001:
                    best_area = safe
                    best_fit = trial
            elif safe is None:
                report_pdf_diagnostic("pdf_table_clearance_unavailable", f"Original box retained: {original}", item.page_idx)
            item.flowables = [flow for flow, _, _, _ in best_fit.measurements]
        top = min(max(original[1], best_area[1]), best_area[3] - best_fit.height)
        item.draw_rect = (best_area[0], top, best_area[0] + best_fit.width, top + best_fit.height)
        item.fit = best_fit
        for flow in item.flowables:
            if not isinstance(flow, SpatialTableContent):
                continue
            size = flow.effective_font_size * best_fit.scale
            source = "html" if flow.failure is None else "fallback"
            report_pdf_diagnostic(
                "pdf_table_layout",
                f"source={source}, angle={flow.angle}, font_size={size:.4f}, "
                f"original_bbox_pt={original}, draw_bbox_pt={item.draw_rect}; {flow.location}",
                item.page_idx,
            )
            if flow.failure is None and size < 6 - 0.001:
                report_pdf_diagnostic(
                    "pdf_layout_small_text", f"Structured table retained below 6 pt: {size:.4f}; {flow.location}", item.page_idx
                )
