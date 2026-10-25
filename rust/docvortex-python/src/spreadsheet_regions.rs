//! 电子表格区域发现的批量边界；运算期间只持有 Rust 自有快照。

use docvortex_core::spreadsheet_regions::{
    self as core, ConnectedRegion, RegionInput, RegionSummary,
};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::sync::atomic::{AtomicUsize, Ordering};

type Point = (i64, i64);
type Span = (i64, i64, i64, i64);
type RegionRecord = (Vec<Point>, (i64, i64, i64, i64));
type CandidateRecord = (i64, f64, Vec<RegionRecord>, Py<PyDict>);
type SelectionRecord = (
    i64,
    f64,
    Vec<RegionRecord>,
    Vec<usize>,
    Vec<CandidateRecord>,
);

static SELECT_BATCHES: AtomicUsize = AtomicUsize::new(0);
static DISCOVERY_BATCHES: AtomicUsize = AtomicUsize::new(0);
static SUBSET_BATCHES: AtomicUsize = AtomicUsize::new(0);

/// 只在返回边界物化区域成员，核心计算不访问 Python 坐标对象。
fn region_records(regions: Vec<ConnectedRegion>) -> Vec<RegionRecord> {
    regions
        .into_iter()
        .map(|region| {
            (
                region.cells,
                (
                    region.row_start,
                    region.row_end,
                    region.col_start,
                    region.col_end,
                ),
            )
        })
        .collect()
}

/// 保留 Python 评分字段及整数类型，诊断路径用于独立逐阶段差分。
fn summary_record(py: Python<'_>, summary: &RegionSummary) -> PyResult<Py<PyDict>> {
    let output = PyDict::new(py);
    output.set_item("real_singleton_ratio", summary.real_singleton_ratio)?;
    output.set_item("weighted_blank_ratio", summary.weighted_blank_ratio)?;
    output.set_item(
        "interior_blank_line_ratio",
        summary.interior_blank_line_ratio,
    )?;
    output.set_item("sparse_large_table_ratio", summary.sparse_large_table_ratio)?;
    output.set_item("severe_separator_count", summary.severe_separator_count)?;
    output.set_item("row_overlap_excess_ratio", summary.row_overlap_excess_ratio)?;
    Ok(output.unbind())
}

/// 一次执行三档区域发现、评分和稳定选择，默认只返回获选区域的成员。
#[pyfunction]
#[pyo3(signature = (starts, content_points, content_rectangles, semantic_positions, spans, max_row, max_col, diagnostics=false))]
pub(super) fn spreadsheet_select_regions(
    py: Python<'_>,
    starts: Vec<Point>,
    content_points: Vec<Point>,
    content_rectangles: Vec<[i64; 4]>,
    semantic_positions: Vec<Point>,
    spans: Vec<Span>,
    max_row: i64,
    max_col: i64,
    diagnostics: bool,
) -> PyResult<SelectionRecord> {
    let input = RegionInput {
        starts,
        content_points,
        content_rectangles,
        semantic_positions,
        spans,
        max_row,
        max_col,
    };
    let result = py
        .detach(move || core::analyze_regions(input, None))
        .map_err(pyo3::exceptions::PyValueError::new_err)?;
    SELECT_BATCHES.fetch_add(1, Ordering::Relaxed);
    let candidates = if diagnostics {
        result
            .candidates
            .into_iter()
            .map(|candidate| {
                Ok((
                    candidate.gap_tolerance,
                    candidate.penalty,
                    region_records(candidate.regions),
                    summary_record(py, &candidate.summary)?,
                ))
            })
            .collect::<PyResult<Vec<_>>>()?
    } else {
        Vec::new()
    };
    Ok((
        result.gap_tolerance,
        result.penalty,
        region_records(result.regions),
        result.kept_indices,
        candidates,
    ))
}

/// 固定 gap 只执行发现，供仍需按原顺序运行语义回调的格式入口使用。
#[pyfunction]
pub(super) fn spreadsheet_discover_regions(
    py: Python<'_>,
    starts: Vec<Point>,
    content_points: Vec<Point>,
    content_rectangles: Vec<[i64; 4]>,
    max_row: i64,
    max_col: i64,
    gap_tolerance: i64,
) -> PyResult<Vec<RegionRecord>> {
    let input = RegionInput {
        starts,
        content_points,
        content_rectangles,
        semantic_positions: Vec::new(),
        spans: Vec::new(),
        max_row,
        max_col,
    };
    let regions = py
        .detach(move || core::discover_connected_regions(&input, gap_tolerance))
        .map_err(pyo3::exceptions::PyValueError::new_err)?;
    DISCOVERY_BATCHES.fetch_add(1, Ordering::Relaxed);
    Ok(region_records(regions))
}

/// 对物化后的真实语义集合批量筛选，保留空集及相等集合的原有严格包含规则。
#[pyfunction]
pub(super) fn spreadsheet_keep_maximal_semantic_sets(
    py: Python<'_>,
    semantic_sets: Vec<Vec<Point>>,
) -> Vec<usize> {
    let indices = py.detach(move || core::keep_maximal_by_semantic_sets(&semantic_sets));
    SUBSET_BATCHES.fetch_add(1, Ordering::Relaxed);
    indices
}

/// 返回成功完成的选择、固定发现及子集筛选批次数，供验收确认实际命中。
#[pyfunction]
pub(super) fn spreadsheet_region_stats() -> (usize, usize, usize) {
    (
        SELECT_BATCHES.load(Ordering::Relaxed),
        DISCOVERY_BATCHES.load(Ordering::Relaxed),
        SUBSET_BATCHES.load(Ordering::Relaxed),
    )
}
