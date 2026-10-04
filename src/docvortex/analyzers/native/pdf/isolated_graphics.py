"""Restores independent vector graphics without native text members, with decorative backgrounds and text still handled by the original rules."""

import statistics

from .geometry import _bbox_area, _bbox_distance, _bbox_overlap_in_first, _bbox_overlap_in_smaller, _bbox_union_many
from .line_layout import _line_effective_height


def isolated_vector_components(source):
    """An independent image is composed of complex two-dimensional paths and does not claim the main text; the edges of the frames intersect slightly to retain the complete multi-color illustration."""
    heights = [_line_effective_height(line, line.bbox) for line in source.lines if line.angle == 0]
    em = statistics.median(heights) if heights else 10.0
    width, height = source.page_size
    area = width * height
    paths = [
        path
        for path in source.path_infos
        if path.form_depth == 0
        and (path.fill_visible or path.stroke_visible)
        and not path.rectangle_bboxes
        and path.segment_count >= 8
        and 0 < _bbox_area(path.bbox) <= 0.06 * area
    ]
    groups = []
    for path in paths:
        matches = [group for group in groups if any(_bbox_distance(path.bbox, other.bbox) <= 0.35 * em for other in group)]
        if not matches:
            groups.append([path])
        else:
            merged = [path, *(member for group in matches for member in group)]
            groups = [group for group in groups if all(group is not match for match in matches)]
            groups.append(merged)
    output = []
    for group in groups:
        box = _bbox_union_many([path.bbox for path in group])
        if (
            not 0.0005 <= _bbox_area(box) / area <= 0.06
            or min(box[2] - box[0], box[3] - box[1]) < 2.5 * em
            or sum(path.segment_count for path in group) < 30
            or not any(
                path.segment_count >= 20 and min(path.bbox[2] - path.bbox[0], path.bbox[3] - path.bbox[1]) >= 1.5 * em
                for path in group
            )
        ):
            continue
        edges = sum((box[0] <= 0.2 * em, box[1] <= 0.2 * em, box[2] >= width - 0.2 * em, box[3] >= height - 0.2 * em))
        # Multiple complex outlines filled with different colors can form a complete illustration that sticks to the edge of the page. A single corner decoration cannot be used to expand the frame.
        colors = {path.fill_rgba for path in group if path.fill_visible}
        if (edges >= 2 and (len(group) < 8 or len(colors) < 3)) or any(
            _bbox_overlap_in_first(line.bbox, box) > 0.1 for line in source.lines
        ):
            continue
        if any(_bbox_overlap_in_smaller(box, raster) > 0.2 for raster in source.image_bboxes):
            continue
        output.append(box)
    return output


def meaningful_small_raster(source, box):
    """Small grid images that are significantly larger than the main text in both directions and do not cover native text lines are retained; thin line and inline text images are rejected."""
    heights = [_line_effective_height(line, line.bbox) for line in source.lines if line.angle == 0]
    if not heights:
        return False
    em = statistics.median(heights)
    return (
        min(box[2] - box[0], box[3] - box[1]) >= 3 * em
        and _bbox_area(box) >= 0.0005 * source.page_size[0] * source.page_size[1]
        and not any(_bbox_overlap_in_first(line.bbox, box) > 0.01 for line in source.lines)
    )
