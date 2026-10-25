//! OFD 表格快照绑定；纯 Rust 运算释放 GIL，返回原始比较预算的精确位置。

use docvortex_core::ofd_tables::{self as core, AxisLine, Budget};
use pyo3::{exceptions::PyValueError, prelude::*};
use std::sync::atomic::{AtomicUsize, Ordering};

type AxisRecord = (bool, f64, f64, f64);
type ComponentRecord = (Vec<Vec<usize>>, i64, bool);
type CellRecord = (Option<Vec<[usize; 4]>>, i64, bool);

static COMPONENT_BATCHES: AtomicUsize = AtomicUsize::new(0);
static TOPOLOGY_BATCHES: AtomicUsize = AtomicUsize::new(0);
static TOUCH_CHECKS: AtomicUsize = AtomicUsize::new(0);
static BOUNDARY_INTERVALS: AtomicUsize = AtomicUsize::new(0);
static BUDGET_FAILURES: AtomicUsize = AtomicUsize::new(0);

/// 将一次传入的数值快照转为自有线段，扫描期间不访问 Python 对象。
fn snapshot(lines: Vec<AxisRecord>) -> Vec<AxisLine> {
    lines
        .into_iter()
        .map(|(horizontal, coordinate, start, end)| AxisLine {
            horizontal,
            coordinate,
            start,
            end,
        })
        .collect()
}

/// 批量计算线段分量，成员保持原始输入索引和稳定顺序。
#[pyfunction]
pub(super) fn ofd_table_components(
    py: Python<'_>,
    lines: Vec<AxisRecord>,
    count: i64,
    limit: i64,
) -> PyResult<ComponentRecord> {
    let budget = Budget::new(count, limit).map_err(PyValueError::new_err)?;
    let lines = snapshot(lines);
    let result = py
        .detach(move || core::components(&lines, budget))
        .map_err(PyValueError::new_err)?;
    COMPONENT_BATCHES.fetch_add(1, Ordering::Relaxed);
    TOUCH_CHECKS.fetch_add(result.actual_checks, Ordering::Relaxed);
    BUDGET_FAILURES.fetch_add(usize::from(result.exceeded), Ordering::Relaxed);
    Ok((
        result.value.unwrap_or_default(),
        result.budget_count,
        result.exceeded,
    ))
}

/// 批量恢复逻辑单元格，拒绝候选和预算超限分别返回，不能混为同一种空结果。
#[pyfunction]
pub(super) fn ofd_table_logical_cells(
    py: Python<'_>,
    xs: Vec<f64>,
    ys: Vec<f64>,
    lines: Vec<AxisRecord>,
    count: i64,
    limit: i64,
) -> PyResult<CellRecord> {
    let budget = Budget::new(count, limit).map_err(PyValueError::new_err)?;
    let lines = snapshot(lines);
    let result = py
        .detach(move || core::logical_cells(&xs, &ys, &lines, budget))
        .map_err(PyValueError::new_err)?;
    TOPOLOGY_BATCHES.fetch_add(1, Ordering::Relaxed);
    BOUNDARY_INTERVALS.fetch_add(result.actual_checks, Ordering::Relaxed);
    BUDGET_FAILURES.fetch_add(usize::from(result.exceeded), Ordering::Relaxed);
    Ok((result.value, result.budget_count, result.exceeded))
}

/// 返回完成的两类批次、实际连接候选、格边区间检查和预算失败次数。
#[pyfunction]
pub(super) fn ofd_table_stats() -> (usize, usize, usize, usize, usize) {
    (
        COMPONENT_BATCHES.load(Ordering::Relaxed),
        TOPOLOGY_BATCHES.load(Ordering::Relaxed),
        TOUCH_CHECKS.load(Ordering::Relaxed),
        BOUNDARY_INTERVALS.load(Ordering::Relaxed),
        BUDGET_FAILURES.load(Ordering::Relaxed),
    )
}
