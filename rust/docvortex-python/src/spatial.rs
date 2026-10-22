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
    /// 一次验证并打包普通浮点片段，其他数值使用同一类的精确聚合入口。
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
    /// 整数、非有限值及极端坐标在 Rust 内执行相同聚合，算术借用解释器以保留精确值和首见来源。
    #[staticmethod]
    fn aggregate_generic(
        py: Python<'_>,
        fragments: Vec<(usize, Vec<Py<PyAny>>, Vec<Py<PyAny>>)>,
        selected: Vec<usize>,
        excluded: Vec<usize>,
        local: Option<Vec<Py<PyAny>>>,
    ) -> PyResult<docvortex_core::annotation_geometry::Output> {
        use std::collections::{HashMap, HashSet};
        if fragments.iter().any(|f| f.1.len() != 4 || f.2.len() != 4)
            || selected.iter().any(|i| *i >= fragments.len())
            || local.as_ref().is_some_and(|b| b.len() != 4)
        {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "invalid annotation query",
            ));
        }
        let operators = py.import("operator")?;
        let builtins = py.import("builtins")?;
        let lt = operators.getattr("lt")?;
        let gt = operators.getattr("gt")?;
        let ge = operators.getattr("ge")?;
        let subtract = operators.getattr("sub")?;
        let divide = operators.getattr("truediv")?;
        let minimum = builtins.getattr("min")?;
        let maximum = builtins.getattr("max")?;
        let combine = |a: [usize; 4], b: [usize; 4]| -> PyResult<[usize; 4]> {
            let mut result = a;
            for axis in 0..4 {
                let before = fragments[a[axis]].1[axis].bind(py);
                let next = fragments[b[axis]].1[axis].bind(py);
                let selected = if axis < 2 {
                    lt.call1((next, before))?
                } else {
                    gt.call1((next, before))?
                };
                if selected.is_truthy()? {
                    result[axis] = b[axis];
                }
            }
            Ok(result)
        };
        let excluded: HashSet<_> = excluded.into_iter().collect();
        let mut positions = HashMap::new();
        let mut lines: Vec<(usize, [usize; 4], usize)> = Vec::new();
        for index in selected {
            let (source, _, bbox) = &fragments[index];
            if excluded.contains(source) {
                continue;
            }
            if let Some(region) = &local {
                if gt
                    .call1((bbox[3].bind(py), region[1].bind(py)))?
                    .is_truthy()?
                {
                    let overlap = maximum.call1((
                        0.0,
                        subtract.call1((
                            minimum.call1((bbox[2].bind(py), region[2].bind(py)))?,
                            maximum.call1((bbox[0].bind(py), region[0].bind(py)))?,
                        ))?,
                    ))?;
                    let shorter = minimum.call1((
                        subtract.call1((bbox[2].bind(py), bbox[0].bind(py)))?,
                        subtract.call1((region[2].bind(py), region[0].bind(py)))?,
                    ))?;
                    let ratio = if gt.call1((&shorter, 0))?.is_truthy()? {
                        divide.call1((overlap, shorter))?
                    } else {
                        0.0_f64.into_pyobject(py)?.into_any()
                    };
                    if ge.call1((ratio, 0.05))?.is_truthy()? {
                        continue;
                    }
                }
            }
            if let Some(&position) = positions.get(source) {
                let line: &mut (usize, [usize; 4], usize) = &mut lines[position];
                line.1 = combine(line.1, [index; 4])?;
                line.2 += 1;
            } else {
                positions.insert(*source, lines.len());
                lines.push((*source, [index; 4], 1));
            }
        }
        let mut union = None;
        for (_, bbox, _) in &lines {
            union = Some(if let Some(previous) = union {
                combine(previous, *bbox)?
            } else {
                *bbox
            });
        }
        Ok((lines, union))
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
