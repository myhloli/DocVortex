//! 将 Python 批量参数转换为自有记录，再交给纯 Rust 核心计算。
// 绑定使用列式数组和原四元组输出，不为私有传输引入新的公开对象类型。
#![allow(clippy::too_many_arguments, clippy::type_complexity)]

use pyo3::prelude::*;

mod conversion;
mod dedup;
mod geometry;
mod pdfium;
mod scripts;
mod spatial;
mod statistics;
mod tables;

/// 注册私有扩展及协议号；公开 Python 接口仍由原模块提供。
#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(geometry::anchor_pairs, module)?)?;
    module.add_function(wrap_pyfunction!(spatial::title_gaps, module)?)?;
    module.add_function(wrap_pyfunction!(spatial::line_neighbors, module)?)?;
    module.add_function(wrap_pyfunction!(dedup::mapping_runs, module)?)?;
    module.add_function(wrap_pyfunction!(pdfium::read_pdfium_chars, module)?)?;
    module.add_function(wrap_pyfunction!(pdfium::read_pdfium_char_batches, module)?)?;
    module.add_class::<pdfium::PdfiumCharacterBatches>()?;
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
    module.add_function(wrap_pyfunction!(tables::table_row_occupancy, module)?)?;
    module.add("PROTOCOL_VERSION", docvortex_core::PROTOCOL_VERSION)?;
    module.add_function(wrap_pyfunction!(scripts::script_roles, module)?)?;
    module.add_function(wrap_pyfunction!(geometry::normalize_boxes, module)?)?;
    module.add_function(wrap_pyfunction!(geometry::visual_runs, module)?)?;
    module.add_function(wrap_pyfunction!(geometry::local_boxes, module)?)?;
    module.add_function(wrap_pyfunction!(geometry::source_rows, module)?)?;
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
