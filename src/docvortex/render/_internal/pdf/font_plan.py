"""Title font size grouping, measurement plan and internal acceptance statistics exported from the original layout."""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from math import ceil
from statistics import median
from typing import Iterator

from reportlab.platypus import Flowable

from ....schema import BlockBase


@dataclass
class BlockFit:
    """Save the actual measurement results. After measuring once, the text will be drawn directly with the same line breaks and proportions."""

    scale: float
    width: float
    height: float
    measurements: list[tuple[Flowable, float, float, float]]
    small_text: bool = False


@dataclass
class PreparedBlock:
    """Save the content object and original frame that are only materialized once, and do not hold or modify the derived fields of the input protocol."""

    page_idx: int
    block: BlockBase
    flowables: list[Flowable]
    x: float
    y: float
    width: float
    height: float
    group: str | None = None
    base_font_size: float | None = None
    target_font_size: float | None = None
    page_height: float = 0.0
    body_base_font_size: float | None = None
    body_font_size: float | None = None
    reference_font_size: float | None = None
    reference_block: PreparedBlock | None = None
    fit: BlockFit | None = None
    draw_rect: tuple[float, float, float, float] | None = None
    geometry_conflict: bool = False
    clearance_unavailable: bool = False

    @property
    def original_rect(self) -> tuple[float, float, float, float]:
        """Returns the original occupied range of the point unit and the upper left origin without writing back the input bbox."""
        return self.x, self.page_height - self.y - self.height, self.x + self.width, self.page_height - self.y


@dataclass
class FontGroupPlan:
    """Record the basic font size and actual coverage within the group for testing and local comparison tools."""

    group: str
    default_font_size: float
    target_font_size: float
    block_count: int
    fitting_count: int = 0
    exception_count: int = 0
    expanded_count: int = 0
    local_increase_count: int = 0
    titles: list[dict] = field(default_factory=list)


_statistics: ContextVar[list[FontGroupPlan] | None] = ContextVar("pdf_font_statistics", default=None)


@contextmanager
def collect_font_plans() -> Iterator[list[FontGroupPlan]]:
    """Isolate this acceptance statistics, do not change the public interface, and do not allow concurrently exported font size plans to contaminate each other."""
    plans: list[FontGroupPlan] = []
    token = _statistics.set(plans)
    try:
        yield plans
    finally:
        _statistics.reset(token)


def record_font_plans(plans: list[FontGroupPlan]) -> None:
    """Submit statistics to the current internal acceptance context only after a successful draw."""
    collected = _statistics.get()
    if collected is not None:
        collected.extend(plans)


def _ceil_font_size(size: float) -> float:
    """Round up to 0.1 pt to ensure that the title is not truncated due to decimal truncation and is lower than the text plus font size increment."""
    return ceil(size * 10 - 1e-8) / 10


def _reference_body(title: PreparedBlock, bodies: list[PreparedBlock], order: dict[int, int]) -> PreparedBlock | None:
    """Priority will be given to selecting subsequent text on the same page and in the same column; if there is no subsequent text, the nearest text in the same column will be selected."""
    x0, y0, x1, y1 = title.original_rect
    candidates = []
    for body in bodies:
        if body.page_idx != title.page_idx:
            continue
        bx0, by0, bx1, by1 = body.original_rect
        overlap = min(x1, bx1) - max(x0, bx0)
        # Small left margin differences allow the first line to be indented; column width is not determined by borrowing from another column or just an edged paragraph.
        if bx0 - 6 <= x0 < bx1 and overlap >= 0.5 * min(x1 - x0, bx1 - bx0):
            candidates.append(body)
    following = [body for body in candidates if order[id(body)] > order[id(title)] and body.original_rect[1] >= y0]
    if following:
        return min(following, key=lambda body: (body.original_rect[1] - y0, order[id(body)]))
    return min(
        candidates,
        key=lambda body: (abs((body.original_rect[1] + body.original_rect[3]) - (y0 + y1)), order[id(body)]),
        default=None,
    )


def plan_font_sizes(blocks: list[PreparedBlock]) -> list[FontGroupPlan]:
    """Set the title target based on the actual text size, and no longer reduce the entire group of titles based on the original frame capacity or 90% coverage rate."""
    bodies = [block for block in blocks if block.body_font_size is not None]
    fallback = median(body.body_font_size for body in bodies) if bodies else 10.5
    order = {id(block): index for index, block in enumerate(blocks)}
    groups: dict[str, list[PreparedBlock]] = defaultdict(list)
    for block in blocks:
        if block.group is not None and block.base_font_size is not None:
            block.reference_block = _reference_body(block, bodies, order)
            block.reference_font_size = block.reference_block.body_font_size if block.reference_block is not None else fallback
            groups[block.group].append(block)
    plans = []
    chapter_targets = []
    for name, members in groups.items():
        if not name.startswith("paragraph_title:"):
            continue
        target = _ceil_font_size(median(member.reference_font_size for member in members) + 2)
        for member in members:
            member.target_font_size = _ceil_font_size(max(target, member.reference_font_size + 2))
            chapter_targets.append(member.target_font_size)
        plans.append(FontGroupPlan(name, min(member.base_font_size for member in members), target, len(members)))
    for name, members in groups.items():
        if not name.startswith("doc_title:"):
            continue
        target = _ceil_font_size(
            max(chapter_targets) + 2 if chapter_targets else median(member.reference_font_size for member in members) + 4
        )
        for member in members:
            member.target_font_size = _ceil_font_size(max(target, member.reference_font_size + 4))
        plans.append(FontGroupPlan(name, min(member.base_font_size for member in members), target, len(members)))
    return plans
