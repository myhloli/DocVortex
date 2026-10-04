"""Coordinate clustering and local physical rule transformation of shared sparse tables, preserving two-way candidate strategies."""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from .contracts import NativeTableInput
from .geometry import normalize_angle, normalize_bbox, page_bbox_to_table_local


@dataclass(frozen=True, slots=True)
class _LocalRule:
    """Save the thin line interval converted to forward table coordinates."""

    orientation: str
    coordinate: float
    start: float
    end: float
    width: float


def cluster_members(
    values: list[float],
    tolerance: float,
) -> list[tuple[float, tuple[float, ...]]]:
    """Cluster coordinates by one-dimensional distance and retain the original members of each cluster."""

    if not values:
        return []
    clusters: list[list[float]] = [[value] for value in sorted(values)[:1]]
    for value in sorted(values)[1:]:
        center = float(statistics.median(clusters[-1]))
        if abs(value - center) <= tolerance:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return [(float(statistics.median(cluster)), tuple(cluster)) for cluster in clusters]


def _local_rules(
    table_input: NativeTableInput,
    width: float,
    height: float,
) -> tuple[_LocalRule, ...]:
    """Convert the drawing line on the page into a regular interval that is reoriented according to the local long axis."""

    table_bbox = normalize_bbox(table_input.table_bbox)
    if table_bbox is None:
        return ()
    angle = normalize_angle(table_input.angle)
    output: list[_LocalRule] = []
    for rule in table_input.drawing_lines:
        bbox = normalize_bbox(rule.bbox)
        if bbox is None:
            continue
        local_bbox = page_bbox_to_table_local(bbox, table_bbox, angle)
        if local_bbox is None:
            continue
        local_width = local_bbox[2] - local_bbox[0]
        local_height = local_bbox[3] - local_bbox[1]
        if local_width >= max(1.0, 3.0 * max(local_height, 0.1)):
            output.append(
                _LocalRule(
                    orientation="horizontal",
                    coordinate=(local_bbox[1] + local_bbox[3]) / 2.0,
                    start=max(0.0, local_bbox[0]),
                    end=min(width, local_bbox[2]),
                    width=max(rule.width, local_height),
                )
            )
        elif local_height >= max(1.0, 3.0 * max(local_width, 0.1)):
            output.append(
                _LocalRule(
                    orientation="vertical",
                    coordinate=(local_bbox[0] + local_bbox[2]) / 2.0,
                    start=max(0.0, local_bbox[1]),
                    end=min(height, local_bbox[3]),
                    width=max(rule.width, local_width),
                )
            )
    return tuple(output)


__all__ = ["_LocalRule", "cluster_members", "_local_rules"]
