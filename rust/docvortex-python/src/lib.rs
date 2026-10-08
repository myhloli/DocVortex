//! 将 Python 批量参数转换为自有记录，再交给纯 Rust 核心计算。
// 绑定使用列式数组和原四元组输出，不为私有传输引入新的公开对象类型。
#![allow(clippy::too_many_arguments, clippy::type_complexity)]

use pyo3::prelude::*;

mod classification;
mod column_cache;
mod conversion;
mod dedup;
mod fill_bands;
mod fonts;
mod fraction_rules;
mod geometry;
mod geometry_document;
mod geometry_risk;
mod geometry_runs;
mod glyph_patch;
mod graphic_numeric;
mod image_numeric;
mod inline_styles;
mod marker_geometry;
mod math_words;
mod pdfium;
mod pixels;
mod profile_context;
mod rule_graphics;
mod rule_tail;
mod script_snapshot;
mod scripts;
mod snapshot;
mod spatial;
mod statistics;
mod table_candidates;
mod table_merge;
mod table_pending;
mod table_rows;
mod tables;
mod text_pipeline;
mod text_projection;
mod tight_text;
mod typography_owned;
mod unmapped_ink;

/// 注册私有扩展及协议号；公开 Python 接口仍由原模块提供。
#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(image_numeric::image_components, module)?)?;
    module.add_function(wrap_pyfunction!(image_numeric::image_contours, module)?)?;
    module.add_function(wrap_pyfunction!(image_numeric::image_resize, module)?)?;
    module.add_function(wrap_pyfunction!(image_numeric::image_warp, module)?)?;
    module.add_function(wrap_pyfunction!(
        image_numeric::image_minimum_rectangle,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(image_numeric::image_morphology, module)?)?;
    module.add_function(wrap_pyfunction!(image_numeric::image_lines, module)?)?;
    module.add_function(wrap_pyfunction!(image_numeric::image_polygons, module)?)?;
    module.add_function(wrap_pyfunction!(image_numeric::image_solve, module)?)?;
    module.add_class::<column_cache::NativeColumnCache>()?;
    module.add_class::<fill_bands::NativeFillBands>()?;
    module.add_function(wrap_pyfunction!(fill_bands::prepare_fill_bands, module)?)?;
    module.add_function(wrap_pyfunction!(
        graphic_numeric::graphic_numeric_lines,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        unmapped_ink::unmapped_formula_ink,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        tight_text::tight_space_candidates,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        marker_geometry::marker_sources_owned,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        marker_geometry::marker_glyphs_owned,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(math_words::math_words_owned, module)?)?;
    module.add_function(wrap_pyfunction!(
        typography_owned::typography_owned,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(rule_graphics::year_header_groups, module)?)?;
    module.add_function(wrap_pyfunction!(pixels::blank_top_bitmap, module)?)?;
    module.add_function(wrap_pyfunction!(
        table_pending::table_pending_glyphs_owned,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        fraction_rules::fraction_members_owned,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        rule_graphics::overlap_member_groups,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(statistics::exact_float_mean, module)?)?;
    module.add_function(wrap_pyfunction!(rule_graphics::code_rule_windows, module)?)?;
    module.add_function(wrap_pyfunction!(
        glyph_patch::glyph_bold_indices_owned,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        profile_context::lane_profile_context_owned,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(table_rows::fragment_rows_owned, module)?)?;
    module.add_function(wrap_pyfunction!(
        rule_graphics::prose_rule_eligible,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        geometry::plain_source_records_owned,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        rule_tail::supported_lane_intervals_owned,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(rule_tail::lane_assignments_owned, module)?)?;
    module.add_function(wrap_pyfunction!(
        geometry_document::owned_font_run_metadata,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        text_projection::project_content_chars_owned,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(rule_graphics::form_state_program, module)?)?;
    module.add_function(wrap_pyfunction!(
        rule_graphics::detached_formula_neighbors,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        rule_graphics::fragment_row_groups,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        rule_graphics::prose_rule_conflicts,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(rule_graphics::compound_baselines, module)?)?;
    module.add_function(wrap_pyfunction!(rule_graphics::form_state_events, module)?)?;
    module.add_function(wrap_pyfunction!(
        rule_graphics::numeric_noise_candidates,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(rule_graphics::raster_axis_groups, module)?)?;
    module.add_function(wrap_pyfunction!(rule_tail::short_tail_lane_events, module)?)?;
    module.add_function(wrap_pyfunction!(
        rule_tail::short_tail_destinations_owned,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        rule_graphics::isolated_path_groups,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        rule_graphics::overpainted_addresses,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        rule_graphics::grow_formula_component,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        rule_graphics::short_tail_destinations,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(inline_styles::inline_style_stats, module)?)?;
    module.add_function(wrap_pyfunction!(pixels::crop_bitmap_bgr, module)?)?;
    module.add_function(wrap_pyfunction!(pixels::glyph_masks, module)?)?;
    module.add_function(wrap_pyfunction!(pixels::blank_top_rgb, module)?)?;
    module.add_function(wrap_pyfunction!(pixels::blank_top_gray, module)?)?;
    module.add_class::<table_merge::OwnedRuleCore>()?;
    module.add_class::<table_merge::NativeTableGrid>()?;
    module.add_class::<table_merge::NativeTableMerger>()?;
    module.add_class::<geometry_risk::NativeGeometryRisk>()?;
    module.add_class::<geometry_risk::NativeGeometryRuns>()?;
    module.add_class::<geometry_document::NativeStyleDocument>()?;
    module.add_function(wrap_pyfunction!(
        geometry_runs::build_geometry_style,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        geometry_runs::build_geometry_runs,
        module
    )?)?;
    module.add_class::<snapshot::NativeTextSnapshot>()?;
    module.add_class::<snapshot::NativeGeometryEvidence>()?;
    module.add_class::<classification::NativeClassificationSnapshot>()?;
    module.add_function(wrap_pyfunction!(
        classification::read_pdfium_classification,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        classification::classification_snapshot_stats,
        module
    )?)?;
    module.add_class::<script_snapshot::NativeScriptEvidence>()?;
    module.add_function(wrap_pyfunction!(
        script_snapshot::script_snapshot_stats,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(snapshot::text_snapshot_stats, module)?)?;
    module.add_function(wrap_pyfunction!(snapshot::geometry_evidence_stats, module)?)?;
    module.add_function(wrap_pyfunction!(
        snapshot::read_pdfium_text_snapshot,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        snapshot::visual_evidence_stage_stats,
        module
    )?)?;
    module.add_class::<fonts::NativeFontProvider>()?;
    module.add_class::<table_candidates::PreparedRuleCandidates>()?;
    module.add_function(wrap_pyfunction!(text_pipeline::group_text_lines, module)?)?;
    module.add_function(wrap_pyfunction!(
        text_pipeline::prepare_visual_lines,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(geometry::anchor_pairs, module)?)?;
    module.add_function(wrap_pyfunction!(spatial::title_gaps, module)?)?;
    module.add_function(wrap_pyfunction!(spatial::line_neighbors, module)?)?;
    module.add_function(wrap_pyfunction!(dedup::mapping_runs, module)?)?;
    module.add_function(wrap_pyfunction!(dedup::mapping_glyph_rows, module)?)?;
    module.add_function(wrap_pyfunction!(pdfium::read_pdfium_chars, module)?)?;
    module.add_function(wrap_pyfunction!(
        pdfium::read_pdfium_char_form_owners,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(pdfium::read_pdfium_char_batches, module)?)?;
    module.add_class::<pdfium::PdfiumCharacterBatches>()?;
    module.add_class::<pdfium::PdfiumVisualCharacterBatches>()?;
    module.add_class::<pdfium::PdfiumObjectBatches>()?;
    module.add_class::<pdfium::PdfiumDrawingLineBatches>()?;
    module.add_function(wrap_pyfunction!(pdfium::read_pdfium_subpaths, module)?)?;
    module.add_function(wrap_pyfunction!(pdfium::read_pdfium_objects, module)?)?;
    module.add_function(wrap_pyfunction!(
        pdfium::read_pdfium_text_visibility,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        pdfium::read_pdfium_text_visibility_with_roots,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(pdfium::read_pdfium_drawing_lines, module)?)?;
    module.add_function(wrap_pyfunction!(
        pdfium::pdfium_drawing_line_stage_stats,
        module
    )?)?;
    module.add_class::<pdfium::PdfiumPathLineBatches>()?;
    module.add_class::<pdfium::PdfiumPathInfoBatches>()?;
    module.add_function(wrap_pyfunction!(pdfium::read_pdfium_path_evidence, module)?)?;
    module.add_function(wrap_pyfunction!(
        pdfium::read_pdfium_visual_batches,
        module
    )?)?;
    module.add("PDFIUM_RECORD_BATCH_SIZE", pdfium::RECORD_BATCH_SIZE)?;
    module.add(
        "PdfiumReadError",
        module.py().get_type::<pdfium::PdfiumReadError>(),
    )?;
    module.add_class::<spatial::BaselineCandidates>()?;
    module.add_class::<tables::TableNoteMetrics>()?;
    module.add_class::<tables::StableColumnClusters>()?;
    module.add_class::<tables::TableRowGeometry>()?;
    module.add_class::<spatial::BaselineGeometryCandidates>()?;
    module.add_class::<spatial::AnnotationGeometry>()?;
    module.add_function(wrap_pyfunction!(scripts::inline_script_matches, module)?)?;
    module.add_function(wrap_pyfunction!(statistics::ordered_clusters, module)?)?;
    module.add_function(wrap_pyfunction!(statistics::typography_metrics, module)?)?;
    module.add_function(wrap_pyfunction!(statistics::lane_gap, module)?)?;
    module.add_function(wrap_pyfunction!(tables::table_visual_rows, module)?)?;
    module.add_function(wrap_pyfunction!(tables::cell_visual_groups_owned, module)?)?;
    module.add_function(wrap_pyfunction!(tables::table_row_occupancy, module)?)?;
    module.add("PROTOCOL_VERSION", docvortex_core::PROTOCOL_VERSION)?;
    module.add_function(wrap_pyfunction!(scripts::script_roles, module)?)?;
    module.add_function(wrap_pyfunction!(geometry::normalize_boxes, module)?)?;
    module.add_function(wrap_pyfunction!(geometry::visual_runs, module)?)?;
    module.add_function(wrap_pyfunction!(geometry::local_boxes, module)?)?;
    module.add_function(wrap_pyfunction!(geometry::source_rows, module)?)?;
    module.add_function(wrap_pyfunction!(geometry::source_rows_plain, module)?)?;
    module.add_function(wrap_pyfunction!(dedup::paint_pairs, module)?)?;
    module.add_function(wrap_pyfunction!(dedup::dedup_components, module)?)?;
    module.add_function(wrap_pyfunction!(dedup::hidden_candidates, module)?)?;
    module.add_function(wrap_pyfunction!(dedup::confirmed_offsets, module)?)?;
    module.add_function(wrap_pyfunction!(tables::table_boxes, module)?)?;
    module.add_function(wrap_pyfunction!(tables::coverage_batch, module)?)?;
    module.add_function(wrap_pyfunction!(tables::merge_rules, module)?)?;
    module.add_function(wrap_pyfunction!(tables::assign_cells, module)?)?;
    module.add_function(wrap_pyfunction!(tables::grid_parents, module)?)?;
    module.add_function(wrap_pyfunction!(tables::component_specs, module)?)?;
    module.add_function(wrap_pyfunction!(geometry::materialize_geometry, module)?)?;
    module.add_function(wrap_pyfunction!(scripts::script_roles_raw, module)?)?;
    module.add_function(wrap_pyfunction!(scripts::script_roles_raw_batch, module)?)?;
    module.add_function(wrap_pyfunction!(scripts::script_roles_plain_batch, module)?)?;
    Ok(())
}
