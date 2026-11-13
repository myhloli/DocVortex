"""跨页表格合并使用的内部状态模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeAlias

MAX_HEADER_ROWS = 5

BlockDict: TypeAlias = dict[str, Any]
PageInfoDict: TypeAlias = dict[str, Any]
CalculationBBox: TypeAlias = tuple[int, int, int, int]


class TableMergeRejection(str, Enum):
    """跨页表格延续检测的拒绝原因。"""

    INVALID_PAGE = "invalid_page"
    NON_CONSECUTIVE_PAGES = "non_consecutive_pages"
    BOUNDARY_TABLE_MISSING = "boundary_table_missing"
    BOUNDARY_BLOCKED = "boundary_blocked"
    PDF_STRUCTURE_PRESERVED = "pdf_structure_preserved"
    INVALID_TABLE_STRUCTURE = "invalid_table_structure"
    CONTINUATION_CAPTION_REQUIRED = "continuation_caption_required"
    MULTIPLE_PREVIOUS_FOOTNOTES = "multiple_previous_footnotes"
    PREVIOUS_FOOTNOTES_REQUIRE_CAPTION = "previous_footnotes_require_caption"
    INCOMPATIBLE_TABLE_WIDTHS = "incompatible_table_widths"
    INCOMPATIBLE_TABLE_STRUCTURE = "incompatible_table_structure"


@dataclass(frozen=True, slots=True)
class TableMergeDecision:
    """记录一次相邻页表格检测的结果，不包含论文正文。"""

    previous_page_idx: int | None
    current_page_idx: int | None
    previous_table_index: int | None
    current_table_index: int | None
    rejection: TableMergeRejection | None
    blocker_page_idx: int | None = None
    blocker_block_type: str | None = None

    @property
    def mergeable(self) -> bool:
        """返回相邻页表格是否通过延续检测。"""
        return self.rejection is None


@dataclass
class RowMetrics:
    """记录单行的有效列、实际列和视觉列指标。"""

    row_idx: int
    effective_cols: int
    actual_cols: int
    visual_cols: int


@dataclass
class RowSignature:
    """记录表头行的列结构与规范化文本签名。"""

    effective_cols: int
    colspans: tuple[int, ...]
    rowspans: tuple[int, ...]
    normalized_texts: tuple[str, ...]
    display_texts: tuple[str, ...]

    @property
    def cell_count(self) -> int:
        """返回签名中的显式单元格数量。"""
        return len(self.colspans)


@dataclass
class RenderedCellSegment:
    """记录渲染单元格的列区间、语义内容及原始跨行范围。

    行范围相对传入的 rows 从零计数，end_row 不包含结束行；虚拟占位的行范围为 None。
    """

    text: str
    start_col: int
    end_col: int
    has_non_text_content: bool = False
    start_row: int | None = None
    end_row: int | None = None


@dataclass
class RowScanResult:
    """封装一次 HTML 行扫描得到的列指标与跨行占位。"""

    row_effective_cols: list[int]
    row_metrics: list[RowMetrics]
    total_cols: int
    last_nonempty_row_metrics: RowMetrics | None
    tail_occupied: dict[int, set[int]]


@dataclass
class TableMergeState:
    """缓存单张表格的 block 所有者、HTML 树和结构指标。"""

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
