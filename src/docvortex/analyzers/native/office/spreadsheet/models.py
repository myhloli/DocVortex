"""Neutral internal data model used during the worksheet projection phase."""

from __future__ import annotations

from typing import Annotated, Any, TypeAlias

from pydantic import BaseModel, Field, NonNegativeInt, PositiveInt
from pydantic.dataclasses import dataclass


CellPosition: TypeAlias = tuple[int, int]
OptionalCellPosition: TypeAlias = tuple[int | None, int | None]
AnchoredBlock: TypeAlias = tuple[CellPosition, int, dict[str, Any]]
FormulaMap: TypeAlias = dict[CellPosition, list[str]]


@dataclass
class DataRegion:
    """Represents the 1-based bounding rectangular area of non-empty cells in the worksheet."""

    min_row: Annotated[PositiveInt, Field(description="Smallest row index (1-based index).")]
    max_row: Annotated[PositiveInt, Field(description="Largest row index (1-based index).")]
    min_col: Annotated[PositiveInt, Field(description="Smallest column index (1-based index).")]
    max_col: Annotated[PositiveInt, Field(description="Largest column index (1-based index).")]

    def width(self) -> PositiveInt:
        """Returns the number of columns in the data range."""
        return self.max_col - self.min_col + 1

    def height(self) -> PositiveInt:
        """Returns the number of rows in the data range."""
        return self.max_row - self.min_row + 1


class ExcelCell(BaseModel):
    """Represents worksheet cells that have completed text, media, and formula materialization."""

    row: int
    col: int
    text: str
    row_span: int
    col_span: int
    styles: dict[str, Any] = Field(default_factory=dict)
    media: list[str] = Field(default_factory=list)
    equations: list[str] = Field(default_factory=list)
    text_is_html: bool = False
    source_row: int | None = None
    source_col: int | None = None


class ExcelTable(BaseModel):
    """Represents a rectangular table with display coordinates and source worksheet anchor points."""

    anchor: tuple[NonNegativeInt, NonNegativeInt]
    num_rows: int
    num_cols: int
    data: list[ExcelCell]


@dataclass(frozen=True, slots=True)
class SheetImage:
    """Represents a picture or picture formula bound to worksheet cell anchor."""

    anchor: OptionalCellPosition
    image_base64: str | None = None
    latex: str | None = None
    order: int = 0


__all__ = [
    "AnchoredBlock",
    "CellPosition",
    "DataRegion",
    "ExcelCell",
    "ExcelTable",
    "FormulaMap",
    "OptionalCellPosition",
    "SheetImage",
]
