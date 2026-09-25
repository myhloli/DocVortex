//! 基线候选、邻行与注释几何的空间索引绑定。

use docvortex_core::geometry::Box4;
use pyo3::prelude::*;

/// 调用内持有只读区间树，不缓存 Python 行对象。
#[pyclass(frozen)]
pub(super) struct BaselineCandidates {
    index: docvortex_core::spatial::IntervalIndex,
}

#[pymethods]
impl BaselineCandidates {
    /// 验证区间后建立纯数值索引，拒绝不完整的分组参数。
    #[new]
    fn new(py: Python<'_>, bounds: Vec<(f64, f64)>, groups: Vec<usize>) -> PyResult<Self> {
        py.detach(move || docvortex_core::spatial::IntervalIndex::new(bounds, groups))
            .map(|index| Self { index })
            .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("invalid interval index"))
    }

    /// 按固定容量查询连续行，最密的一行也不会造成全页平方内存。
    fn rows(&self, py: Python<'_>, start: usize, count: usize, budget: usize) -> Vec<Vec<usize>> {
        py.detach(|| self.index.rows(start, count.min(64), budget.min(8192)))
    }
}

/// 在一次调用内过滤同行几何，保留原始行框的独立准入路径。
#[pyclass(frozen)]
pub(super) struct BaselineGeometryCandidates {
    index: docvortex_core::spatial::BaselineGeometry,
}

#[pymethods]
impl BaselineGeometryCandidates {
    /// 创建验证后的只读索引，拒绝不支持的传输记录。
    #[new]
    fn new(
        py: Python<'_>,
        bounds: Vec<(f64, f64)>,
        groups: Vec<usize>,
        boxes: Vec<Box4>,
        heights: Vec<f64>,
        sources: Vec<Option<Box4>>,
    ) -> PyResult<Self> {
        py.detach(|| {
            docvortex_core::spatial::BaselineGeometry::new(bounds, groups, boxes, heights, sources)
        })
        .map(|index| Self { index })
        .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("unsupported baseline geometry"))
    }
    /// 返回当前原始行序的有限批次，不保存整页行对。
    fn rows(&self, py: Python<'_>, start: usize, count: usize, budget: usize) -> Vec<Vec<usize>> {
        py.detach(|| self.index.rows(start, count, budget))
    }
}

/// 持有走廊片段的纯数值快照，不保留 Python 或 PDF 句柄。
#[pyclass(frozen)]
pub(super) struct AnnotationGeometry {
    index: docvortex_core::annotation_geometry::AnnotationGeometry,
}

#[pymethods]
impl AnnotationGeometry {
    /// 一次验证并打包片段几何，未知输入由 Python 参考实现处理。
    #[new]
    fn new(
        py: Python<'_>,
        fragments: Vec<docvortex_core::annotation_geometry::Fragment>,
    ) -> PyResult<Self> {
        py.detach(|| docvortex_core::annotation_geometry::AnnotationGeometry::new(fragments))
            .map(|index| Self { index })
            .ok_or_else(|| {
                pyo3::exceptions::PyValueError::new_err("unsupported annotation geometry")
            })
    }
    /// 返回保序来源和坐标索引；非法查询不会静默产生部分结果。
    fn aggregate(
        &self,
        py: Python<'_>,
        selected: Vec<usize>,
        excluded: Vec<usize>,
        local: Option<Box4>,
    ) -> PyResult<docvortex_core::annotation_geometry::Output> {
        py.detach(|| self.index.aggregate(selected, excluded, local))
            .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("invalid annotation query"))
    }
}

/// 冻结一页行级数值后批量计算上下物理净空。
#[pyfunction]
pub(super) fn title_gaps(
    py: Python<'_>,
    records: Vec<(usize, Option<usize>, Box4, f64, bool)>,
) -> Option<Vec<(Option<f64>, Option<f64>)>> {
    py.detach(move || docvortex_core::spatial::title_gaps(records))
}

/// 返回邻行索引，供 Python 按原顺序追加原始分析对象。
#[pyfunction]
pub(super) fn line_neighbors(
    py: Python<'_>,
    records: Vec<(usize, Box4, f64, f64)>,
) -> Option<Vec<(Option<usize>, Option<usize>)>> {
    py.detach(move || docvortex_core::spatial::line_neighbors(records))
}
