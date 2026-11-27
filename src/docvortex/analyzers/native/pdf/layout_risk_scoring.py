"""按物理页将 Flash 原生版面的结构冲突换算成伪置信度。"""

from __future__ import annotations

from functools import partial

from ....schema import BBox
from .geometry import _bbox_area
from .layout_fragment_risk import fragmented_region
from .layout_grouping_risk import displaced_note_points, grouping_loss_points
from .layout_native_conflicts import nested_block_risk, table_container_risk
from .layout_overlap_risk import MAX_MINOR_OVERLAP_POINTS, minor_overlap_points
from .layout_program_risk import program_equation_conflict
from .layout_risk import extract_layout_risk
from .layout_spatial_confidence import spatial_fragmentation
from .layout_vector_risk import vector_conflict, wants_vectors


def score_layout_pages(
    pages: list[list[dict]],
    *,
    program_controls: dict[int, tuple[BBox, ...]] | None = None,
    vector_geometry=None,
    source_lines=None,
) -> tuple[float, ...]:
    """每页只取最严重的结构冲突，按原页序返回 0 至 1 的伪置信度。"""
    confidences = []
    for index, blocks in enumerate(pages):
        factors = extract_layout_risk(blocks, separate_severe=True)
        points = max(
            (factors[k][0] for k in ("formula_ownership", "grid_fragmentation", "table_structure") if k in factors), default=0
        )
        if program_controls and (controls := program_controls.get(index)) and program_equation_conflict(blocks, controls):
            points = max(points, 30)
        if fragmented_region(blocks):
            points = max(points, 60)
        if vector_geometry and not points and wants_vectors(blocks):
            size, geometry = vector_geometry(index)
            read_lines = partial(source_lines, index) if source_lines is not None else None
            if vector_conflict(blocks, size, geometry, source_lines=read_lines):
                points = 60
        if spatial := spatial_fragmentation(blocks):
            points = max(points, spatial[0])
        if nested := nested_block_risk(blocks):
            points = max(points, nested[0])
        if (
            source_lines is not None
            and vector_geometry is not None
            and any(
                block.get("type") == "table" and not block.get("angle") and _bbox_area(block["bbox"]) >= 0.08
                for block in blocks
            )
        ):
            size, geometry = vector_geometry(index)
            if table := table_container_risk(blocks, size, source_lines(index), geometry):
                points = max(points, table[0])
        # 在严重证据检查完成后才加入轻度扣分，避免跳过原生矢量检查或重复扣分。
        if points < MAX_MINOR_OVERLAP_POINTS:
            points = max(points, minor_overlap_points(blocks))
        if points < 30:
            read_lines = partial(source_lines, index) if source_lines is not None else None
            points = max(points, grouping_loss_points(blocks, read_lines))
        if points < 9:
            points = max(points, displaced_note_points(blocks))
        # 先在整数尺度取补值，让每个百分位都能与相同的十进制配置精确比较。
        confidences.append((100 - points) / 100)
    return tuple(confidences)
