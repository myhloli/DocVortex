"""PDF 表格的兼容入口；实现按检测、注释和物化职责组织。"""

from .table_annotations import (
    _build_table_annotation as _build_table_annotation,
    _collect_caption_rows as _collect_caption_rows,
    _collect_footnote_rows as _collect_footnote_rows,
    _extract_auxiliary_table_note_marker as _extract_auxiliary_table_note_marker,
    _table_core_references_marker as _table_core_references_marker,
    _line_has_compact_marker_token as _line_has_compact_marker_token,
    _line_has_superscript_marker as _line_has_superscript_marker,
    _table_note_body_reference_height as _table_note_body_reference_height,
    _visual_row_text as _visual_row_text,
    _is_table_note_text as _is_table_note_text,
    _find_table_caption as _find_table_caption,
    _find_caption_number_peers as _find_caption_number_peers,
    _merge_table_candidate_annotations as _merge_table_candidate_annotations,
)
from .table_constants import (
    _TABLE_CAPTION_RE as _TABLE_CAPTION_RE,
    _TABLE_CONTINUATION_RE as _TABLE_CONTINUATION_RE,
    _TABLE_NOTE_RE as _TABLE_NOTE_RE,
    _AUXILIARY_TABLE_NOTE_RE as _AUXILIARY_TABLE_NOTE_RE,
    _TABLE_SPLIT_NUMBER_RE as _TABLE_SPLIT_NUMBER_RE,
    _FILLED_GRID_MIN_PAGE_AREA_RATIO as _FILLED_GRID_MIN_PAGE_AREA_RATIO,
    _FILLED_GRID_MAX_PAGE_AREA_RATIO as _FILLED_GRID_MAX_PAGE_AREA_RATIO,
    _FILLED_GRID_MIN_PAGE_WIDTH_RATIO as _FILLED_GRID_MIN_PAGE_WIDTH_RATIO,
    _FILLED_GRID_MIN_PAGE_HEIGHT_RATIO as _FILLED_GRID_MIN_PAGE_HEIGHT_RATIO,
)
from .table_detection import (
    _detect_table_candidates as _detect_table_candidates,
    _externalize_table_continuation_captions as _externalize_table_continuation_captions,
)
from .table_filled_grid import (
    _detect_filled_grid_table_candidates as _detect_filled_grid_table_candidates,
    _select_maximal_filled_grid_cells as _select_maximal_filled_grid_cells,
    _bbox_is_contained_with_tolerance as _bbox_is_contained_with_tolerance,
    _group_filled_grid_cells_into_bands as _group_filled_grid_cells_into_bands,
    _filled_grid_band_covers_outer_width as _filled_grid_band_covers_outer_width,
)
from .table_rows import (
    _clip_visual_row_to_corridor as _clip_visual_row_to_corridor,
)
from .table_materialization import (
    _recover_native_table_html as _recover_native_table_html,
    _materialize_table_blocks as _materialize_table_blocks,
    _materialize_table_annotations as _materialize_table_annotations,
    _split_table_annotation_visual_groups as _split_table_annotation_visual_groups,
    _build_table_annotation_blocks as _build_table_annotation_blocks,
    _merge_table_annotation_content as _merge_table_annotation_content,
    _table_body_materialization_bbox as _table_body_materialization_bbox,
    _candidate_projection_line_indices as _candidate_projection_line_indices,
    _expand_candidate_same_baseline_members as _expand_candidate_same_baseline_members,
)
from .table_rules import (
    _build_fragments as _build_fragments,
    _cluster_fragment_rows as _cluster_fragment_rows,
    _build_rule_table_candidates as _build_rule_table_candidates,
    _expand_candidates_to_connected_rule_grids as _expand_candidates_to_connected_rule_grids,
    _build_closed_rule_grid_candidates as _build_closed_rule_grid_candidates,
    _closed_grid_vertical_track_positions as _closed_grid_vertical_track_positions,
    _count_occupied_closed_grid_columns as _count_occupied_closed_grid_columns,
    _connected_rule_grid_bboxes as _connected_rule_grid_bboxes,
    _connected_rule_grid_components as _connected_rule_grid_components,
    _connected_horizontal_rule_bboxes as _connected_horizontal_rule_bboxes,
    _rule_bands_share_grid_tracks as _rule_bands_share_grid_tracks,
    _group_long_horizontal_rules as _group_long_horizontal_rules,
    _rows_inside_rule_interval as _rows_inside_rule_interval,
    _every_rule_interval_has_multi_cell_row as _every_rule_interval_has_multi_cell_row,
    _rule_intervals_are_column_compatible as _rule_intervals_are_column_compatible,
    _continuous_table_row_segments as _continuous_table_row_segments,
    _table_segment_reaches_boundaries as _table_segment_reaches_boundaries,
    _table_rows_align_with_rule_span as _table_rows_align_with_rule_span,
    _count_aligned_vertical_rules as _count_aligned_vertical_rules,
    _compact_fully_ruled_grid_column_count as _compact_fully_ruled_grid_column_count,
    _full_height_vertical_rule_positions as _full_height_vertical_rule_positions,
    _looks_like_page_column_prose as _looks_like_page_column_prose,
    _count_repeated_fill_bands as _count_repeated_fill_bands,
    _longest_dense_multi_cell_rows as _longest_dense_multi_cell_rows,
    _expand_rule_table_candidate as _expand_rule_table_candidate,
    _count_stable_columns as _count_stable_columns,
    _merge_table_candidates as _merge_table_candidates,
    _median_fragment_height as _median_fragment_height,
)
from ....document.pdf.text.contracts import (
    Char as Char,
)
from .table_recovery import (
    NativeTableInput as NativeTableInput,
    NativeTableRectangle as NativeTableRectangle,
    NativeTableRule as NativeTableRule,
    coerce_native_table_rectangles as coerce_native_table_rectangles,
    coerce_native_table_rules as coerce_native_table_rules,
    recover_native_pdf_table as recover_native_pdf_table,
)
from .table_text_styles import (
    render_native_table_html_with_scripts as render_native_table_html_with_scripts,
)
from ....foundation.text import (
    merge_text_line_contents as merge_text_line_contents,
)
from .spatial_text import (
    project_pdf_table_text as project_pdf_table_text,
)
from ....schema import (
    BBox as BBox,
)
from ....document.pdf.document import (
    PDFPathInfo as PDFPathInfo,
)
from .models import (
    _Fragment as _Fragment,
    _LineItem as _LineItem,
    _LocalAxisLine as _LocalAxisLine,
    _PageSource as _PageSource,
    _TableAnnotation as _TableAnnotation,
    _TableCandidate as _TableCandidate,
    _VisualRow as _VisualRow,
)
from .geometry import (
    _bbox_area as _bbox_area,
    _bbox_axis_overlap_ratio as _bbox_axis_overlap_ratio,
    _bbox_center_x as _bbox_center_x,
    _bbox_center_y as _bbox_center_y,
    _bbox_overlap_in_smaller as _bbox_overlap_in_smaller,
    _bbox_union as _bbox_union,
    _bbox_union_many as _bbox_union_many,
    _coerce_bbox as _coerce_bbox,
    _expand_bbox as _expand_bbox,
    _point_in_bbox as _point_in_bbox,
    _rotate_bbox_from_upright as _rotate_bbox_from_upright,
    _rotate_bbox_to_upright as _rotate_bbox_to_upright,
    _transform_axis_lines as _transform_axis_lines,
)
from .line_layout import (
    _font_signatures_share_family as _font_signatures_share_family,
    _line_effective_height as _line_effective_height,
    _line_tight_output_bbox as _line_tight_output_bbox,
)
from .line_merging import (
    _same_baseline_geometry as _same_baseline_geometry,
)
from .native_text import (
    _normalize_native_run_text as _normalize_native_run_text,
)
