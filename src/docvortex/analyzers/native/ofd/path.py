"""Parse OFD PathObject and extract the axial line segments available in the table."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from loguru import logger
from lxml import etree  # type: ignore[reportMissingImports]

from ....schema import BBox
from .constants import MAX_PATH_COMMANDS, MAX_PATH_TOKENS
from .errors import OfdResourceLimitError
from .geometry import Affine, bbox_intersection, parse_affine, parse_st_box, transform_bbox
from .models import AxisLine
from .package import element_text, first_descendant, parse_int

_TOKEN_RE = re.compile(r"CM|[SMLQBAC]|[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


@dataclass(slots=True)
class OfdPathBudget:
    """Cumulative limit compaction path token and number of commands."""

    command_count: int = 0
    token_count: int = 0

    def charge_token(self) -> None:
        """Accumulate the actual scanned path token and fail when it exceeds the limit."""
        self.token_count += 1
        if self.token_count > MAX_PATH_TOKENS:
            raise OfdResourceLimitError(f"OFD resource limit exceeded: max_path_tokens={MAX_PATH_TOKENS}")

    def charge_command(self) -> None:
        """The path commands actually scanned are accumulated and fail when the limit is exceeded."""
        self.command_count += 1
        if self.command_count > MAX_PATH_COMMANDS:
            raise OfdResourceLimitError(f"OFD resource limit exceeded: max_path_commands={MAX_PATH_COMMANDS}")


def _finite_float(value: str) -> float | None:
    """Convert the path token into a finite floating point number."""
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _line_bbox(first: tuple[float, float], second: tuple[float, float], width: float) -> tuple[BBox, str] | None:
    """Convert approximately horizontal or vertical line segments to non-degenerate bbox."""
    dx = second[0] - first[0]
    dy = second[1] - first[1]
    length = math.hypot(dx, dy)
    if length <= 0:
        return None
    tolerance = max(0.2, 0.02 * length)
    thickness = max(width, 0.1)
    if abs(dy) <= tolerance:
        return (
            (
                min(first[0], second[0]),
                min(first[1], second[1]) - thickness / 2,
                max(first[0], second[0]),
                max(first[1], second[1]) + thickness / 2,
            ),
            "horizontal",
        )
    if abs(dx) <= tolerance:
        return (
            (
                min(first[0], second[0]) - thickness / 2,
                min(first[1], second[1]),
                max(first[0], second[0]) + thickness / 2,
                max(first[1], second[1]),
            ),
            "vertical",
        )
    return None


PathCommand = tuple[str, tuple[float, ...]]


def parse_path_commands(value: str, budget: OfdPathBudget) -> tuple[PathCommand, ...]:
    """Only scan the complete path command once, and the drawing and table detection share the same limited numerical result."""
    commands: list[PathCommand] = []
    operator: str | None = None
    parameters: list[float] = []
    arity = {"S": 2, "M": 2, "L": 2, "Q": 4, "B": 6, "A": 7, "CM": 2}
    cursor = 0
    pending = False
    for match in _TOKEN_RE.finditer(value):
        budget.charge_token()
        if value[cursor : match.start()].strip(" \t\r\n,"):
            return ()
        cursor = match.end()
        token = match.group()
        if token in {*arity, "C"}:
            if parameters:
                return ()
            budget.charge_command()
            operator = token
            pending = operator != "C"
            if operator == "C":
                commands.append((operator, ()))
                operator = None
            continue
        if operator is None:
            return ()
        number = _finite_float(token)
        if number is None:
            return ()
        parameters.append(number)
        if len(parameters) == arity[operator]:
            if operator == "A" and (
                parameters[0] < 0 or parameters[1] < 0 or parameters[3] not in (0, 1) or parameters[4] not in (0, 1)
            ):
                return ()
            commands.append((operator, tuple(parameters)))
            parameters.clear()
            pending = False
    return () if pending or parameters or value[cursor:].strip() else tuple(commands)


def command_segments(
    commands: tuple[PathCommand, ...], transform: Affine
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Read the straight line segment from the parsed path, and the curve only updates the endpoint and does not use the control line as the table border."""
    result = []
    current = start = None
    for operator, values in commands:
        if operator == "C":
            if current is not None and start is not None and current != start:
                result.append((transform.apply(current), transform.apply(start)))
            current = start
            continue
        endpoint = values[-2:]
        if operator in {"S", "M"}:
            current = start = endpoint
        else:
            if operator in {"L", "CM"} and current is not None:
                result.append((transform.apply(current), transform.apply(endpoint)))
            current = endpoint
    return result


def _segments(value: str, transform: Affine, budget: OfdPathBudget) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Retain the existing internal entrance and reuse the complete command to parse and extract straight line segments."""
    return command_segments(parse_path_commands(value, budget), transform)


def build_axis_lines(
    path_object: etree._Element,
    *,
    parent_transform: Affine,
    parent_clip: BBox,
    paint_order: int,
    template_id: int | None,
    budget: OfdPathBudget,
    resolved_style: dict[str, str] | None = None,
    commands: tuple[PathCommand, ...] | None = None,
) -> list[AxisLine]:
    """Extract visible axial segments from a PathObject."""
    style = resolved_style or {}
    if (style.get("Visible") or path_object.get("Visible") or "true").casefold() in {"false", "0"}:
        return []
    if (style.get("Alpha") or path_object.get("Alpha") or "255").strip() == "0":
        return []
    boundary = parse_st_box(path_object.get("Boundary"))
    if boundary is None:
        return []
    boundary_page = transform_bbox(boundary, parent_transform)
    if boundary_page is None:
        return []
    object_clip = bbox_intersection(boundary_page, parent_clip)
    if object_clip is None:
        return []
    try:
        width = max(0.1, float(style.get("LineWidth") or path_object.get("LineWidth") or 0.353))
    except ValueError:
        width = 0.353
    object_transform = parent_transform.compose(Affine.translation(boundary[0], boundary[1])).compose(
        parse_affine(path_object.get("CTM"))
    )
    data = first_descendant(path_object, "AbbreviatedData")
    if commands is None:
        commands = parse_path_commands(element_text(data), budget) if data is not None else ()
    if any(operator in {"Q", "B", "A"} for operator, _ in commands):
        return []
    segments = command_segments(commands, object_transform)
    stroke = style.get("Stroke", path_object.get("Stroke", "true")).casefold() not in {"false", "0"}
    fill = style.get("Fill", path_object.get("Fill", "false")).casefold() in {"true", "1"}
    extracted: list[AxisLine] = []
    if fill and not stroke:
        # Only a single elongated rectangle is reduced to the center line, and the glyph outline, background and corner filling cannot provide border evidence.
        if len(segments) != 4 or not commands or commands[-1][0] != "C":
            return []
        points = {point for segment in segments for point in segment}
        xs, ys = sorted({p[0] for p in points}), sorted({p[1] for p in points})
        if len(points) != 4 or len(xs) != 2 or len(ys) != 2:
            return []
        if any(first[0] != second[0] and first[1] != second[1] for first, second in segments):
            return []
        dx, dy = xs[-1] - xs[0], ys[-1] - ys[0]
        if min(dx, dy) <= 0 or max(dx, dy) < 5 * min(dx, dy):
            return []
        orientation = "horizontal" if dx >= dy else "vertical"
        clipped = bbox_intersection((xs[0], ys[0], xs[-1], ys[-1]), object_clip)
        return [AxisLine(clipped, orientation, min(dx, dy), paint_order, template_id)] if clipped else []
    if not stroke:
        return []
    width *= max(math.hypot(object_transform.a, object_transform.b), math.hypot(object_transform.c, object_transform.d))
    for first, second in segments:
        line = _line_bbox(first, second, width)
        if line is not None:
            line_bbox, orientation = line
            clipped = bbox_intersection(line_bbox, object_clip)
            if clipped is not None:
                extracted.append(AxisLine(clipped, orientation, width, paint_order, template_id))
    if not extracted and parse_int(path_object.get("ID")) is None:
        logger.debug("OFD_PATH_SKIPPED: path without stable ID produced no axis lines")
    return extracted


__all__ = ["OfdPathBudget", "build_axis_lines"]
