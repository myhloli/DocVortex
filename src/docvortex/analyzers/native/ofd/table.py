"""Based on OFD native horizontal and vertical lines and text boxes, the high-confidence full-line table is restored."""

from __future__ import annotations

import html
from dataclasses import dataclass, replace

from ....schema import BBox
from .constants import MAX_TABLE_INTERSECTION_CHECKS
from .errors import OfdResourceLimitError
from .geometry import bbox_area, bbox_center, bbox_intersection
from .models import AxisLine, ImageItem, TextLine
from .assembly import line_html, merge_same_baseline_lines

_COORD_TOLERANCE = 0.6
_INTERSECTION_TOLERANCE = 0.8
_MAX_GRID_CELLS = 10_000
_MAX_TABLE_AXIS_LINES = 2_000


@dataclass(frozen=True, slots=True)
class OfdTableCell:
    """Record the basic cell range occupied by logical cells, and the end index is not included."""

    row: int
    column: int
    end_row: int
    end_column: int


@dataclass(frozen=True, slots=True)
class OfdTableRegion:
    """Save a materialized OFD table and its consumed text lines."""

    bbox: BBox
    html: str
    paint_order: int
    consumed_line_ids: frozenset[int]
    consumed_image_ids: frozenset[int] = frozenset()
    xs: tuple[float, ...] = ()
    ys: tuple[float, ...] = ()
    text_lines: tuple[TextLine, ...] = ()
    cells: tuple[OfdTableCell, ...] = ()


@dataclass(slots=True)
class OfdTableBudget:
    """Cumulatively limit the number of intersection comparisons of table line segments of the entire OFD."""

    intersection_check_count: int = 0

    def charge_intersection_check(self) -> None:
        """Accumulate one line segment intersection comparison and fail when it exceeds the limit."""
        self.intersection_check_count += 1
        if self.intersection_check_count > MAX_TABLE_INTERSECTION_CHECKS:
            raise OfdResourceLimitError(
                f"OFD resource limit exceeded: max_table_intersection_checks={MAX_TABLE_INTERSECTION_CHECKS}"
            )


def _line_coord(line: AxisLine) -> float:
    """Returns the center coordinate of the axial line segment in the vertical direction."""
    if line.orientation == "horizontal":
        return (line.bbox[1] + line.bbox[3]) / 2.0
    return (line.bbox[0] + line.bbox[2]) / 2.0


def _line_interval(line: AxisLine) -> tuple[float, float]:
    """Return the interval of the axial line segment along its own direction."""
    return (line.bbox[0], line.bbox[2]) if line.orientation == "horizontal" else (line.bbox[1], line.bbox[3])


def _touches(first: AxisLine, second: AxisLine) -> bool:
    """Determine whether two axial line segments intersect or are connected collinearly."""
    if first.orientation == second.orientation:
        if abs(_line_coord(first) - _line_coord(second)) > _COORD_TOLERANCE:
            return False
        first_interval = _line_interval(first)
        second_interval = _line_interval(second)
        return min(first_interval[1], second_interval[1]) + _INTERSECTION_TOLERANCE >= max(
            first_interval[0], second_interval[0]
        )
    horizontal = first if first.orientation == "horizontal" else second
    vertical = second if first.orientation == "horizontal" else first
    x = _line_coord(vertical)
    y = _line_coord(horizontal)
    return (
        horizontal.bbox[0] - _INTERSECTION_TOLERANCE <= x <= horizontal.bbox[2] + _INTERSECTION_TOLERANCE
        and vertical.bbox[1] - _INTERSECTION_TOLERANCE <= y <= vertical.bbox[3] + _INTERSECTION_TOLERANCE
    )


def _normalize_axis_lines(lines: list[AxisLine]) -> list[AxisLine]:
    """Merge repeated and adjacent segments according to axial and adjacent orbits, and sort them before linear scanning."""
    tracks: list[list[AxisLine]] = []
    for line in sorted(lines, key=lambda item: (item.orientation, _line_coord(item), _line_interval(item))):
        if (
            tracks
            and line.orientation == tracks[-1][0].orientation
            and abs(_line_coord(line) - _line_coord(tracks[-1][0])) <= _COORD_TOLERANCE
        ):
            tracks[-1].append(line)
        else:
            tracks.append([line])
    output = []
    for track in tracks:
        coordinate = sum(_line_coord(line) for line in track) / len(track)
        merged: list[tuple[float, float]] = []
        for start, end in sorted(_line_interval(line) for line in track):
            if merged and start <= merged[-1][1] + _INTERSECTION_TOLERANCE:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        width = max(line.width for line in track)
        for start, end in merged:
            box = (
                (start, coordinate - width / 2, end, coordinate + width / 2)
                if track[0].orientation == "horizontal"
                else (coordinate - width / 2, start, coordinate + width / 2, end)
            )
            output.append(replace(track[0], bbox=box, width=width, paint_order=min(line.paint_order for line in track)))
    return output


def _components(lines: list[AxisLine], budget: OfdTableBudget) -> list[list[AxisLine]]:
    """Construct deterministic connected components according to the intersection relationship of line segments."""
    parents = list(range(len(lines)))

    def find(index: int) -> int:
        """Find and compress a union-find root node."""
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(first: int, second: int) -> None:
        """Merge two line segment components."""
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parents[max(first_root, second_root)] = min(first_root, second_root)

    for first_index, first in enumerate(lines):
        for second_index in range(first_index + 1, len(lines)):
            budget.charge_intersection_check()
            if _touches(first, lines[second_index]):
                union(first_index, second_index)
    grouped: dict[int, list[AxisLine]] = {}
    for index, line in enumerate(lines):
        grouped.setdefault(find(index), []).append(line)
    return list(grouped.values())


def _cluster_coordinates(values: list[float]) -> list[float]:
    """Aggregate neighbor line coordinates into stable grid orbits."""
    clusters: list[list[float]] = []
    for value in sorted(values):
        if not clusters or value - clusters[-1][-1] > _COORD_TOLERANCE:
            clusters.append([value])
        else:
            clusters[-1].append(value)
    return [sum(cluster) / len(cluster) for cluster in clusters]


def _covers_interval(lines: list[AxisLine], start: float, end: float) -> bool:
    """Determine whether the collinear segments basically cover a given interval."""
    intervals = sorted(_line_interval(line) for line in lines)
    cursor = start
    for left, right in intervals:
        if right < cursor - _INTERSECTION_TOLERANCE:
            continue
        if left > cursor + _INTERSECTION_TOLERANCE:
            return False
        cursor = max(cursor, right)
        if cursor >= end - _INTERSECTION_TOLERANCE:
            return True
    return cursor >= end - _INTERSECTION_TOLERANCE


def _component_grid(component: list[AxisLine]) -> tuple[list[float], list[float], BBox] | None:
    """Recover grid tracks with complete outer frames from line segment components."""
    horizontal = [line for line in component if line.orientation == "horizontal"]
    vertical = [line for line in component if line.orientation == "vertical"]
    if len(horizontal) < 2 or len(vertical) < 2:
        return None
    xs = _cluster_coordinates([_line_coord(line) for line in vertical])
    ys = _cluster_coordinates([_line_coord(line) for line in horizontal])
    if len(xs) < 2 or len(ys) < 2 or (len(xs) - 1) * (len(ys) - 1) > _MAX_GRID_CELLS:
        return None
    left, right = xs[0], xs[-1]
    top, bottom = ys[0], ys[-1]
    outer_horizontal = [line for line in horizontal if abs(_line_coord(line) - top) <= _COORD_TOLERANCE]
    outer_horizontal += [line for line in horizontal if abs(_line_coord(line) - bottom) <= _COORD_TOLERANCE]
    outer_vertical = [line for line in vertical if abs(_line_coord(line) - left) <= _COORD_TOLERANCE]
    outer_vertical += [line for line in vertical if abs(_line_coord(line) - right) <= _COORD_TOLERANCE]
    top_ok = _covers_interval(
        [line for line in outer_horizontal if abs(_line_coord(line) - top) <= _COORD_TOLERANCE],
        left,
        right,
    )
    bottom_ok = _covers_interval(
        [line for line in outer_horizontal if abs(_line_coord(line) - bottom) <= _COORD_TOLERANCE], left, right
    )
    left_ok = _covers_interval(
        [line for line in outer_vertical if abs(_line_coord(line) - left) <= _COORD_TOLERANCE],
        top,
        bottom,
    )
    right_ok = _covers_interval(
        [line for line in outer_vertical if abs(_line_coord(line) - right) <= _COORD_TOLERANCE], top, bottom
    )
    if not (top_ok and bottom_ok and left_ok and right_ok):
        return None
    return xs, ys, (left, top, right, bottom)


def _cell_index(values: list[float], coordinate: float) -> int | None:
    """Return the adjacent track interval index where the coordinates are located."""
    for index, (start, end) in enumerate(zip(values[:-1], values[1:], strict=True)):
        if start - _COORD_TOLERANCE <= coordinate <= end + _COORD_TOLERANCE:
            return index
    return None


def _logical_cells(
    xs: list[float], ys: list[float], lines: list[AxisLine], budget: OfdTableBudget
) -> tuple[OfdTableCell, ...] | None:
    """Connect the basic grid according to the real dividing edges, and reject local broken edges and non-rectangular merged areas."""
    rows, columns = len(ys) - 1, len(xs) - 1
    parents = list(range(rows * columns))

    def root(index: int) -> int:
        """Find and compress the connected area to which the basic grid belongs."""
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def boundary_state(orientation: str, coordinate: float, start: float, end: float) -> int:
        """Return complete edges, missing edges, or uncertain partial edges, and compare budgets cumulatively."""
        aligned = []
        for line in lines:
            budget.charge_intersection_check()
            if line.orientation == orientation and abs(_line_coord(line) - coordinate) <= _COORD_TOLERANCE:
                aligned.append(line)
        if _covers_interval(aligned, start, end):
            return 1
        if any(
            min(_line_interval(line)[1], end) - max(_line_interval(line)[0], start) > _INTERSECTION_TOLERANCE
            for line in aligned
        ):
            return -1
        return 0

    present_edges: list[tuple[int, int]] = []
    for row in range(rows):
        for column in range(columns):
            current = row * columns + column
            neighbors = []
            if column + 1 < columns:
                neighbors.append((current + 1, "vertical", xs[column + 1], ys[row], ys[row + 1]))
            if row + 1 < rows:
                neighbors.append((current + columns, "horizontal", ys[row + 1], xs[column], xs[column + 1]))
            for neighbor, orientation, coordinate, start, end in neighbors:
                state = boundary_state(orientation, coordinate, start, end)
                if state < 0:
                    return None
                if state == 0:
                    parents[root(neighbor)] = root(current)
                else:
                    present_edges.append((current, neighbor))
    if any(root(first) == root(second) for first, second in present_edges):
        return None
    groups: dict[int, list[tuple[int, int]]] = {}
    for index in range(rows * columns):
        groups.setdefault(root(index), []).append(divmod(index, columns))
    cells = []
    for members in groups.values():
        top, left = min(r for r, c in members), min(c for r, c in members)
        bottom, right = max(r for r, c in members) + 1, max(c for r, c in members) + 1
        if (bottom - top) * (right - left) != len(members):
            return None
        cells.append(OfdTableCell(top, left, bottom, right))
    return tuple(sorted(cells, key=lambda cell: (cell.row, cell.column)))


def _image_cell(table: OfdTableRegion, image: ImageItem) -> tuple[int, int] | None:
    """Only claim decodable pictures that completely fall into a unique logical cell, allowing you to cross the virtual track inside the cell."""
    if image.image_base64 is None:
        return None
    x0, y0, x1, y1 = image.bbox
    matches = [
        cell
        for cell in table.cells
        if table.xs[cell.column] - _COORD_TOLERANCE <= x0
        and x1 <= table.xs[cell.end_column] + _COORD_TOLERANCE
        and table.ys[cell.row] - _COORD_TOLERANCE <= y0
        and y1 <= table.ys[cell.end_row] + _COORD_TOLERANCE
    ]
    return (matches[0].row, matches[0].column) if len(matches) == 1 else None


def _cell_lines(table: OfdTableRegion) -> dict[tuple[int, int], list[TextLine]]:
    """First, allocate original text fragments according to cells to avoid splicing across table boundaries."""
    cells: dict[tuple[int, int], list[TextLine]] = {}
    occupancy = {
        (r, c): (cell.row, cell.column)
        for cell in table.cells
        for r in range(cell.row, cell.end_row)
        for c in range(cell.column, cell.end_column)
    }
    for line in table.text_lines:
        center_x, center_y = bbox_center(line.bbox)
        column = _cell_index(list(table.xs), center_x)
        row = _cell_index(list(table.ys), center_y)
        if row is not None and column is not None:
            cells.setdefault(occupancy[(row, column)], []).append(line)
    return {key: merge_same_baseline_lines(lines) for key, lines in cells.items()}


def _serialize_grid(table: OfdTableRegion, images: list[ImageItem]) -> OfdTableRegion:
    """Output text and pictures according to the spatial order within the cell, and return the successfully claimed picture collection."""
    cells: dict[tuple[int, int], list[tuple[BBox, int, str]]] = {}
    for key, lines in _cell_lines(table).items():
        cells[key] = [(line.bbox, line.paint_order, line_html(line)) for line in lines if line.text.strip()]
    consumed_images: set[int] = set()
    for image in images:
        key = _image_cell(table, image)
        if key is None:
            continue
        source = html.escape(image.image_base64 or "", quote=True)
        width = max(1, round((image.bbox[2] - image.bbox[0]) * 96 / 25.4))
        markup = f'<img src="{source}" width="{width}">'
        cells.setdefault(key, []).append((image.bbox, image.paint_order, markup))
        consumed_images.add(id(image))
    anchors = {(cell.row, cell.column): cell for cell in table.cells}
    output = ["<table>"]
    for row in range(len(table.ys) - 1):
        output.append("<tr>")
        for column in range(len(table.xs) - 1):
            cell = anchors.get((row, column))
            if cell is None:
                continue
            attributes = ""
            if cell.end_row - row > 1:
                attributes += f' rowspan="{cell.end_row - row}"'
            if cell.end_column - column > 1:
                attributes += f' colspan="{cell.end_column - column}"'
            items = sorted(cells.get((row, column), []), key=lambda item: (item[0][1], item[0][0], item[1]))
            content = "<br>".join(item[2] for item in items)
            output.append(f"<td{attributes}>{content}</td>")
        output.append("</tr>")
    output.append("</table>")
    return replace(table, html="".join(output), consumed_image_ids=frozenset(consumed_images))


def recover_tables(
    axis_lines: list[AxisLine],
    text_lines: list[TextLine],
    budget: OfdTableBudget,
    *,
    images: list[ImageItem] | None = None,
) -> list[OfdTableRegion]:
    """Recover non-overlapping high-confidence tables from page axial line segments."""
    axis_lines = _normalize_axis_lines(axis_lines)
    if len(axis_lines) < 4 or len(text_lines) < 2 or len(axis_lines) > _MAX_TABLE_AXIS_LINES:
        return []
    candidates: list[OfdTableRegion] = []
    for component in _components(axis_lines, budget):
        grid = _component_grid(component)
        if grid is None:
            continue
        xs, ys, bbox = grid
        logical_cells = _logical_cells(xs, ys, component, budget)
        if logical_cells is None:
            continue
        contained: list[TextLine] = []
        for line in text_lines:
            center_x, center_y = bbox_center(line.bbox)
            if (
                bbox_intersection(line.bbox, bbox) is not None
                and bbox[0] <= center_x <= bbox[2]
                and bbox[1] <= center_y <= bbox[3]
            ):
                contained.append(line)
        if len(contained) < 2:
            continue
        candidates.append(
            OfdTableRegion(
                bbox=bbox,
                html="",
                cells=logical_cells,
                xs=tuple(xs),
                ys=tuple(ys),
                text_lines=tuple(contained),
                paint_order=min((line.paint_order for line in component), default=0),
                consumed_line_ids=frozenset(id(line) for line in contained),
            )
        )
    selected: list[OfdTableRegion] = []
    for candidate in sorted(candidates, key=lambda item: (-bbox_area(item.bbox), item.bbox[1], item.bbox[0])):
        if any(bbox_intersection(candidate.bbox, existing.bbox) is not None for existing in selected):
            continue
        if sum(len(lines) for lines in _cell_lines(candidate).values()) >= 2:
            selected.append(candidate)
    return [
        _serialize_grid(table, images or [])
        for table in sorted(selected, key=lambda item: (item.bbox[1], item.bbox[0], item.paint_order))
    ]


__all__ = ["OfdTableBudget", "OfdTableRegion", "recover_tables"]
