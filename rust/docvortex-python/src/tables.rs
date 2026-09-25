//! 表格计算与调用内索引状态的 Python 绑定。

use docvortex_core::geometry::Box4;
use docvortex_core::tables;
use pyo3::prelude::*;
use pyo3::types::PyList;

use super::conversion::{box_tuple, read_boxes, BoxTuple};

/// 行范围极值索引，不向 Python 返回重复浮点坐标。
#[pyclass(frozen)]
pub(super) struct TableRowGeometry {
    index: docvortex_core::row_geometry::RowGeometry,
}

#[pymethods]
impl TableRowGeometry {
    /// 构建时一次验证全部有限框，非法输入必须交回参考实现。
    #[new]
    fn new(py: Python<'_>, boxes: Vec<[f64; 4]>) -> PyResult<Self> {
        py.detach(|| docvortex_core::row_geometry::RowGeometry::new(boxes))
            .map(|index| Self { index })
            .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("nonfinite row geometry"))
    }

    /// 返回四个坐标分别来自哪一行，Python 复用已有坐标对象。
    fn union_indices(&self, py: Python<'_>, start: usize, end: usize) -> Option<[usize; 4]> {
        py.detach(|| self.index.union_indices(start, end))
    }

    /// 保留原行顺序，只跳过下边界一定不超过表底的前缀。
    fn first_after(&self, py: Python<'_>, bottom: f64) -> Option<usize> {
        if !bottom.is_finite() {
            return None;
        }
        Some(py.detach(|| self.index.first_after(bottom)))
    }
}

/// 调用内的稳定列累计状态，均值读取不折叠补偿量。
#[pyclass]
pub(super) struct StableColumnClusters {
    state: docvortex_core::columns::Columns,
}

#[pymethods]
impl StableColumnClusters {
    /// Python 边界依据解释器版本选择已验证的浮点求和模式。
    #[new]
    fn new(compensated: bool) -> Self {
        Self {
            state: docvortex_core::columns::Columns::new(compensated),
        }
    }

    /// 只传入严格前缀之后的新增行，纯数值计算期间释放 GIL。
    fn extend(
        &mut self,
        py: Python<'_>,
        rows: Vec<Vec<(f64, f64)>>,
        tolerance: f64,
    ) -> Option<(usize, f64)> {
        py.detach(|| self.state.extend(&rows, tolerance))
    }

    /// 为差分测试返回均值、累计结果、成员数和覆盖行数，不暴露 Python 对象。
    fn snapshot(&self) -> Vec<Vec<(f64, f64, usize, usize)>> {
        self.state
            .groups
            .iter()
            .map(|group| {
                group
                    .iter()
                    .map(|c| {
                        let sum = if c.lo != 0.0 && c.lo.is_finite() {
                            c.hi + c.lo
                        } else {
                            c.hi
                        };
                        (c.mean, sum, c.count, c.rows)
                    })
                    .collect()
            })
            .collect()
    }
}

/// 为整页候选复用排序后的正文高度，查询仅传入区间及核心来源。
#[pyclass(frozen)]
pub(super) struct TableNoteMetrics {
    metrics: std::sync::Arc<docvortex_core::statistics::NoteMetrics>,
}

/// 已验证走廊行的来源成员及其中心范围，只在当前候选组复用。
#[pyclass(frozen)]
pub(super) struct TableNoteCore {
    rows: docvortex_core::note_index::CoreRows,
    owner: std::sync::Arc<docvortex_core::statistics::NoteMetrics>,
}

#[pymethods]
impl TableNoteMetrics {
    /// 一次性接收全部走廊行成员，后续候选仅传入行区间。
    fn prepare_rows(&self, py: Python<'_>, members: Vec<Vec<i64>>) -> TableNoteCore {
        TableNoteCore {
            rows: py.detach(|| self.metrics.rows(members)),
            owner: self.metrics.clone(),
        }
    }

    /// 跳过 Python 成员校验与打包，索引不适用时仍由 Rust 精确排除来源。
    fn height_for_rows(
        &self,
        py: Python<'_>,
        rows: &TableNoteCore,
        start: usize,
        end: usize,
        top: f64,
        bottom: f64,
        fallback: f64,
    ) -> Option<f64> {
        if !std::sync::Arc::ptr_eq(&self.metrics, &rows.owner) {
            return None;
        }
        py.detach(|| {
            self.metrics
                .height_for_rows(&rows.rows, start, end, top, bottom, fallback)
        })
    }
    /// 拒绝非有限数值，确保参考实现负责特殊排序语义。
    #[new]
    fn new(py: Python<'_>, items: Vec<(i64, f64, f64)>) -> PyResult<Self> {
        py.detach(move || docvortex_core::statistics::NoteMetrics::new(items))
            .map(|metrics| Self {
                metrics: std::sync::Arc::new(metrics),
            })
            .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("invalid note metrics"))
    }

    /// 数值筛选时释放 GIL，有限输入溢出返回参考路径标志。
    fn height(
        &self,
        py: Python<'_>,
        top: f64,
        bottom: f64,
        core: Vec<i64>,
        fallback: f64,
    ) -> Option<f64> {
        py.detach(|| self.metrics.height(top, bottom, &core, fallback))
    }
}

/// 表格组行只返回成员索引，由 Python 复用原字形对象。
#[pyfunction]
pub(super) fn table_visual_rows(
    py: Python<'_>,
    boxes: Vec<Box4>,
    ids: Vec<usize>,
    median_height: f64,
) -> Option<Vec<Vec<usize>>> {
    py.detach(move || tables::visual_rows(boxes, ids, median_height))
}

/// 已物化的字符中心按整表轨道批量统计，不改变候选数量。
#[pyfunction]
pub(super) fn table_row_occupancy(
    py: Python<'_>,
    rows: Vec<Vec<f64>>,
    tracks: Vec<f64>,
) -> Option<Vec<Vec<usize>>> {
    py.detach(move || tables::row_occupancy(rows, tracks))
}

/// 表格区域一次筛选整页字符，特殊输入继续沿 Python 的原验证入口处理。
#[pyfunction]
pub(super) fn table_boxes(
    py: Python<'_>,
    values: &Bound<'_, PyList>,
    table: Box4,
    angle: i32,
    fallback: &Bound<'_, PyAny>,
) -> PyResult<Option<Vec<(usize, BoxTuple, Option<BoxTuple>)>>> {
    if table.iter().any(|v| !v.is_finite()) {
        return Ok(None);
    }
    let values = read_boxes(values, fallback)?;
    Ok(Some(py.detach(move || {
        tables::select_boxes(values, table, angle)
            .into_iter()
            .map(|(i, b, l)| (i, box_tuple(b), l.map(box_tuple)))
            .collect()
    })))
}

/// 对整批轨道查询计算覆盖率，避免逐线段绑定调用。
#[pyfunction]
pub(super) fn coverage_batch(
    py: Python<'_>,
    rules: Vec<tables::Rule>,
    queries: Vec<tables::Query>,
) -> Option<Vec<f64>> {
    py.detach(move || tables::coverage_batch(rules, queries))
}

/// 按 Python 簇坐标批量合并相邻线段。
#[pyfunction]
pub(super) fn merge_rules(
    py: Python<'_>,
    rules: Vec<tables::Rule>,
    coordinates: Vec<(u8, Vec<f64>)>,
    tolerance: f64,
    join: f64,
) -> Option<Vec<tables::Rule>> {
    py.detach(move || tables::merge_rules(rules, coordinates, tolerance, join))
}

/// 批量完成所有字符的单元格分配，保留索引和歧义位。
#[pyfunction]
pub(super) fn assign_cells(
    py: Python<'_>,
    glyphs: Vec<Box4>,
    specs: Vec<Box4>,
    index: Option<tables::GridIndex>,
) -> Option<Vec<(Option<usize>, bool)>> {
    py.detach(move || tables::assign_cells(glyphs, specs, index))
}

/// 验证原子格索引后一次执行网格连接。
#[pyfunction]
pub(super) fn grid_parents(
    py: Python<'_>,
    count: usize,
    pairs: Vec<(usize, usize)>,
) -> PyResult<Vec<usize>> {
    if pairs.iter().any(|(a, b)| *a >= count || *b >= count) {
        return Err(pyo3::exceptions::PyIndexError::new_err(
            "cell index out of range",
        ));
    }
    Ok(py.detach(move || tables::grid_parents(count, pairs)))
}

/// 将合法并查集转为矩形单元格，同时返回路径压缩后的父节点。
#[pyfunction]
pub(super) fn component_specs(
    py: Python<'_>,
    parents: Vec<usize>,
    rows: usize,
    cols: usize,
) -> PyResult<(Vec<usize>, Option<Vec<(usize, usize, usize, usize)>>)> {
    if rows.checked_mul(cols) != Some(parents.len()) || parents.iter().any(|i| *i >= parents.len())
    {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "invalid table parents",
        ));
    }
    // 来源是私有 Python 并查集；在绑定边界阻止异常环导致原生死循环。
    for start in 0..parents.len() {
        let mut i = start;
        let mut steps = 0;
        while parents[i] != i {
            i = parents[i];
            steps += 1;
            if steps >= parents.len() {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "cyclic table parents",
                ));
            }
        }
    }
    Ok(py.detach(move || tables::component_specs(parents, rows, cols)))
}
