"""Internal state model used by cross-page table merging."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias

MAX_HEADER_ROWS = 5

BlockDict: TypeAlias = dict[str, Any]
PageInfoDict: TypeAlias = dict[str, Any]
CalculationBBox: TypeAlias = tuple[int, int, int, int]


@dataclass
class RowMetrics:
    """Records the effective column, actual column, and visual column metrics for a single row."""

    row_idx: int
    effective_cols: int
    actual_cols: int
    visual_cols: int


@dataclass
class RowSignature:
    """Record the column structure and normalized text signature of the header row."""

    effective_cols: int
    colspans: tuple[int, ...]
    rowspans: tuple[int, ...]
    normalized_texts: tuple[str, ...]
    display_texts: tuple[str, ...]

    @property
    def cell_count(self) -> int:
        """Returns the number of explicit cells in the signature."""
        return len(self.colspans)


@dataclass
class RenderedCellSegment:
    """Records the visual column interval covered by a rendered cell."""

    text: str
    start_col: int
    end_col: int


@dataclass
class RowScanResult:
    """Encapsulates the column index and cross-row occupancy obtained by a HTML row scan."""

    row_effective_cols: list[int]
    row_metrics: list[RowMetrics]
    total_cols: int
    last_nonempty_row_metrics: RowMetrics | None
    tail_occupied: dict[int, set[int]]


@dataclass
class TableMergeState:
    """Cache block owner, HTML tree and structure indicators for a single table."""

    owner_block: BlockDict | None
    body_block: BlockDict | None
    soup: Any
    tbody: Any
    rows: list[Any]
    total_cols: int
    front_header_info: list[RowSignature]
    front_first_data_row_metrics: dict[int, RowMetrics]
    last_data_row_metrics: RowMetrics | None
    row_effective_cols: list[int]
    tail_occupied: dict[int, set[int]]
    dirty: bool = False
