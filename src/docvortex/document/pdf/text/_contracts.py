# Portions derived from pdftext 0.7.1, Copyright Vik Paruchuri, Apache-2.0.
# Changed in DocVortex: owned character types and source-index mappings replace upstream containers.
"""DocVortex has its own PDF character and geometric data, and does not carry the PDFium handle."""

from __future__ import annotations

from typing import Any, TypedDict


class Bbox:
    __slots__ = ("bbox", "ensure_nonzero_area")

    def __init__(self, bbox: list[float], ensure_nonzero_area: bool = False) -> None:
        """Create independent rectangular objects and ensure area as needed."""
        if ensure_nonzero_area:
            bbox = list(bbox)
            bbox[2] = max(bbox[0], bbox[2] + 1)
            bbox[3] = max(bbox[1], bbox[3] + 1)
        self.bbox = bbox
        self.ensure_nonzero_area = ensure_nonzero_area

    def __getitem__(self, item: int | slice) -> float | list[float]:
        """Read the coordinates of the rectangle."""
        return self.bbox[item]

    def __repr__(self) -> str:
        """Returns a human-readable representation of the rectangle."""
        return f"Bbox({self.bbox})"

    def __reduce__(self) -> tuple:
        # ensure_nonzero_area is already applied at construction; don't re-apply on unpickle
        """Save rectangular values to support cross-process serialization."""
        return (Bbox, (self.bbox,))

    def copy(self) -> Bbox:
        """Duplicate the rectangle to avoid sharing accumulated state."""
        return Bbox(list(self.bbox))

    @property
    def height(self) -> float:
        """Calculates the height geometric properties of a rectangle."""
        return self.bbox[3] - self.bbox[1]

    @property
    def width(self) -> float:
        """Calculates the width geometric properties of a rectangle."""
        return self.bbox[2] - self.bbox[0]

    @property
    def area(self) -> float:
        """Calculates the area geometric properties of a rectangle."""
        return self.width * self.height

    @property
    def center(self) -> list[float]:
        """Calculates the center geometric properties of a rectangle."""
        return [(self.bbox[0] + self.bbox[2]) / 2, (self.bbox[1] + self.bbox[3]) / 2]

    @property
    def size(self) -> list[float]:
        """Calculates the size geometric properties of a rectangle."""
        return [self.width, self.height]

    @property
    def x_start(self) -> float:
        """Calculates the x_start geometric properties of a rectangle."""
        return self.bbox[0]

    @property
    def y_start(self) -> float:
        """Calculates the y_start geometric properties of a rectangle."""
        return self.bbox[1]

    @property
    def x_end(self) -> float:
        """Calculates the x_end geometric properties of a rectangle."""
        return self.bbox[2]

    @property
    def y_end(self) -> float:
        """Calculates the y_end geometric properties of a rectangle."""
        return self.bbox[3]

    def merge(self, other: Bbox) -> Bbox:
        """Returns a new object covering both rectangles."""
        self_bbox = self.bbox
        other_bbox = other.bbox
        return Bbox(
            [
                min(self_bbox[0], other_bbox[0]),
                min(self_bbox[1], other_bbox[1]),
                max(self_bbox[2], other_bbox[2]),
                max(self_bbox[3], other_bbox[3]),
            ]
        )

    def merge_inplace(self, other: Bbox) -> Bbox:
        # Mutates this bbox; only safe on accumulator bboxes that aren't shared
        """Only the current accumulation rectangle is modified."""
        self_bbox = self.bbox
        other_bbox = other.bbox
        if other_bbox[0] < self_bbox[0]:
            self_bbox[0] = other_bbox[0]
        if other_bbox[1] < self_bbox[1]:
            self_bbox[1] = other_bbox[1]
        if other_bbox[2] > self_bbox[2]:
            self_bbox[2] = other_bbox[2]
        if other_bbox[3] > self_bbox[3]:
            self_bbox[3] = other_bbox[3]
        return self

    def overlap_x(self, other: Bbox) -> float:
        """Calculates the overlap_x geometric properties of a rectangle."""
        return max(0, min(self.bbox[2], other.bbox[2]) - max(self.bbox[0], other.bbox[0]))

    def overlap_y(self, other: Bbox) -> float:
        """Calculates the overlap_y geometric properties of a rectangle."""
        return max(0, min(self.bbox[3], other.bbox[3]) - max(self.bbox[1], other.bbox[1]))

    def intersection_area(self, other: Bbox) -> float:
        """Calculates the intersection_area geometric properties of a rectangle."""
        return self.overlap_x(other) * self.overlap_y(other)

    def intersection_pct(self, other: Bbox) -> float:
        """Calculates the intersection_pct geometric properties of a rectangle."""
        if self.area <= 0:
            return 0

        intersection = self.intersection_area(other)
        return intersection / self.area

    def rotate(self, page_width: float, page_height: float, rotation: int) -> Bbox:
        """Convert the rectangle to rotated page coordinates."""
        if rotation not in [0, 90, 180, 270]:
            raise ValueError("Rotation must be one of [0, 90, 180, 270] degrees.")

        x_min, y_min, x_max, y_max = self.bbox

        if rotation == 0:
            return Bbox(list(self.bbox))
        elif rotation == 90:
            new_x_min = page_height - y_max
            new_y_min = x_min
            new_x_max = page_height - y_min
            new_y_max = x_max
        elif rotation == 180:
            new_x_min = page_width - x_max
            new_y_min = page_height - y_max
            new_x_max = page_width - x_min
            new_y_max = page_height - y_min
        elif rotation == 270:
            new_x_min = y_min
            new_y_min = page_width - x_max
            new_x_max = y_max
            new_y_max = page_width - x_min

        # Ensure that x_min < x_max and y_min < y_max; must stay a list so
        # merge_inplace can mutate it
        rotated_bbox = [
            min(new_x_min, new_x_max),
            min(new_y_min, new_y_max),
            max(new_x_min, new_x_max),
            max(new_y_min, new_y_max),
        ]

        return Bbox(rotated_bbox)


class _CharValue(TypedDict):
    bbox: Bbox
    char: str
    rotation: float
    font: dict[str, Any]
    char_idx: int


class Char(_CharValue, total=False):
    """Character and raw index mapping, missing geometry is left as explicit null."""

    source_indices: tuple[int, ...]
    raw_code: int
    text_object_id: int | None
    text_render_mode: int | None
    text_is_visible: bool
    writing_angle: float
    loose_bbox: tuple[float, float, float, float] | None
    tight_bbox: tuple[float, float, float, float] | None
    origin: tuple[float, float] | None


class Span(TypedDict):
    """Base font fragment containing decoded text and raw character references."""

    bbox: Bbox
    text: str
    font: dict[str, Any]
    chars: list[Char]
    char_start_idx: int
    char_end_idx: int
    rotation: float
    url: str
    superscript: bool
    subscript: bool


class Line(TypedDict):
    """Basic text lines, geometry and fragment order remain traceable."""

    spans: list[Span]
    bbox: Bbox
    rotation: float


Spans = list[Span]
Lines = list[Line]
__all__ = ["Bbox", "Char", "Span", "Line", "Spans", "Lines"]
