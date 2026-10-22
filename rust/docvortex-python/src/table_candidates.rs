//! 表格候选连续计算的薄绑定，仅在结果边界物化整数成员与来源索引。
use docvortex_core::table_candidates::RuleCandidates;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// 返回原框坐标的首见极值索引；精确整数与 NaN 使用解释器比较，不复制数值。
pub(super) fn union_source_indices(
    py: Python<'_>,
    boxes: &[Vec<Py<PyAny>>],
) -> PyResult<Option<[usize; 4]>> {
    if boxes.iter().any(|b| b.len() != 4) {
        return Err(PyValueError::new_err("invalid row geometry"));
    }
    if boxes.is_empty() {
        return Ok(None);
    }
    let operators = py.import("operator")?;
    let lt = operators.getattr("lt")?;
    let gt = operators.getattr("gt")?;
    let mut indices = [0; 4];
    for index in 1..boxes.len() {
        for axis in 0..4 {
            let previous = boxes[indices[axis]][axis].bind(py);
            let next = boxes[index][axis].bind(py);
            let better = if axis < 2 {
                lt.call1((next, previous))?
            } else {
                gt.call1((next, previous))?
            };
            if better.is_truthy()? {
                indices[axis] = index;
            }
        }
    }
    Ok(Some(indices))
}

#[pyclass(frozen)]
pub(super) struct PreparedRuleCandidates {
    state: RuleCandidates,
}

#[pymethods]
impl PreparedRuleCandidates {
    /// 非索引输入也由 Rust 分配闭区间，bisect 与相等判断保留宿主数值语义。
    #[staticmethod]
    fn partition_generic(
        py: Python<'_>,
        centers: &Bound<'_, pyo3::types::PyList>,
        rows: Vec<Py<PyAny>>,
    ) -> PyResult<Vec<Vec<usize>>> {
        let mut groups = vec![Vec::new(); centers.len().saturating_sub(1)];
        if centers.len() < 2 {
            return Ok(groups);
        }
        let bisect = py.import("bisect")?.getattr("bisect_left")?;
        let equal = py.import("operator")?.getattr("eq")?;
        for (index, value) in rows.iter().enumerate() {
            let position = bisect
                .call1((centers, value.bind(py)))?
                .extract::<usize>()?;
            if position < centers.len()
                && equal
                    .call1((centers.get_item(position)?, value.bind(py)))?
                    .is_truthy()?
            {
                if position > 0 {
                    groups[position - 1].push(index);
                }
                if position < groups.len() {
                    groups[position].push(index);
                }
            } else if position > 0 && position - 1 < groups.len() {
                groups[position - 1].push(index);
            }
        }
        Ok(groups)
    }

    /// 没有共享走廊索引时，仍在 Rust 合并任意整数来源及原框极值索引。
    #[staticmethod]
    fn core_generic<'py>(
        py: Python<'py>,
        rules: Vec<Vec<Py<PyAny>>>,
        rows: Vec<(Vec<Py<PyAny>>, Vec<Py<PyAny>>)>,
    ) -> PyResult<(Bound<'py, pyo3::types::PySet>, [usize; 4])> {
        use pyo3::types::PySet;
        let members = PySet::empty(py)?;
        let rule_count = rules.len();
        let mut indices = union_source_indices(py, &rules)?
            .ok_or_else(|| PyValueError::new_err("empty rule bounds"))?;
        let mut row_boxes = Vec::with_capacity(rows.len());
        for (sources, bbox) in rows {
            for source in sources {
                members.add(source.bind(py))?;
            }
            row_boxes.push(bbox);
        }
        let row_indices = union_source_indices(py, &row_boxes)?
            .ok_or_else(|| PyValueError::new_err("empty candidate core"))?;
        let operators = py.import("operator")?;
        let lt = operators.getattr("lt")?;
        let gt = operators.getattr("gt")?;
        for axis in 0..4 {
            let previous = rules[indices[axis]][axis].bind(py);
            let next = row_boxes[row_indices[axis]][axis].bind(py);
            let better = if axis < 2 {
                lt.call1((next, previous))?
            } else {
                gt.call1((next, previous))?
            };
            if better.is_truthy()? {
                indices[axis] = rule_count + row_indices[axis];
            }
        }
        Ok((members, indices))
    }

    /// 普通与特殊行中心都用稳定排序分段，返回原行索引而不重写数据对象。
    #[staticmethod]
    fn segments_generic(
        py: Python<'_>,
        centers: &Bound<'_, pyo3::types::PyList>,
        boxes: Vec<Vec<Py<PyAny>>>,
        height: &Bound<'_, PyAny>,
    ) -> PyResult<Vec<Vec<usize>>> {
        use pyo3::types::{PyDict, PyList};
        if boxes.len() != centers.len() || boxes.iter().any(|b| b.len() != 4) {
            return Err(PyValueError::new_err("invalid row segment geometry"));
        }
        let order = PyList::new(py, 0..boxes.len())?;
        let keywords = PyDict::new(py);
        keywords.set_item("key", centers.getattr("__getitem__")?)?;
        order.call_method("sort", (), Some(&keywords))?;
        let operators = py.import("operator")?;
        let subtract = operators.getattr("sub")?;
        let gt = operators.getattr("gt")?;
        let limit = operators.getattr("mul")?.call1((3.0, height))?;
        let maximum = py.import("builtins")?.getattr("max")?;
        let mut groups: Vec<Vec<usize>> = Vec::new();
        for index in order.extract::<Vec<usize>>()? {
            let separate = if let Some(previous) = groups.last().and_then(|g| g.last()) {
                gt.call1((
                    maximum.call1((
                        0.0,
                        subtract.call1((boxes[index][1].bind(py), boxes[*previous][3].bind(py)))?,
                    ))?,
                    &limit,
                ))?
                .is_truthy()?
            } else {
                true
            };
            if separate {
                groups.push(vec![index]);
            } else {
                groups.last_mut().unwrap().push(index);
            }
        }
        Ok(groups)
    }

    /// Python 准入验证之后一次创建不可变走廊数据，计算错误显式传播。
    #[new]
    fn new(
        py: Python<'_>,
        centers: Vec<f64>,
        rows: Vec<(f64, usize, [f64; 4], Vec<i64>)>,
    ) -> PyResult<Self> {
        py.detach(|| RuleCandidates::new(centers, rows))
            .map(|state| Self { state })
            .map_err(PyValueError::new_err)
    }
    /// 保留核心成员于 Rust，不为候选注释和合并提前创建 Python 集合。
    fn owned_core(
        &self,
        py: Python<'_>,
        start: usize,
        end: usize,
        rules: Vec<[f64; 4]>,
    ) -> PyResult<(super::table_merge::OwnedRuleCore, [usize; 4])> {
        let (members, sources) = py
            .detach(|| self.state.core(start, end, &rules))
            .map_err(PyValueError::new_err)?;
        Ok((
            super::table_merge::OwnedRuleCore {
                members: std::sync::Arc::new(members.into_iter().collect()),
            },
            sources,
        ))
    }
    /// 一个原生调用同时完成闭区间分配与首区间表头例外判定。
    fn partition(
        &self,
        py: Python<'_>,
        start: usize,
        end: usize,
        first: usize,
        rule_count: usize,
        height: f64,
    ) -> PyResult<(Vec<Vec<usize>>, bool)> {
        py.detach(|| self.state.partition(start, end, first, rule_count, height))
            .map_err(PyValueError::new_err)
    }
    /// 一次跨边界取得闭区间证据和完整物理行分段，复用同一走廊输入状态。
    fn partition_with_segments(
        &self,
        py: Python<'_>,
        start: usize,
        end: usize,
        first: usize,
        rule_count: usize,
        height: f64,
    ) -> PyResult<(Vec<Vec<usize>>, bool, Vec<Vec<usize>>, bool)> {
        py.detach(|| {
            let (groups, accepted) = self
                .state
                .partition(start, end, first, rule_count, height)?;
            let segments = self.state.segments(start, end, height)?;
            let short = self.state.short_intervals(first, rule_count, height)?;
            Ok::<_, &'static str>((groups, accepted, segments, short))
        })
        .map_err(PyValueError::new_err)
    }
    /// 在已准备走廊上完成成员去重和核心几何展开，返回原对象来源索引。
    fn core(
        &self,
        py: Python<'_>,
        start: usize,
        end: usize,
        rules: Vec<[f64; 4]>,
    ) -> PyResult<(Vec<i64>, [usize; 4])> {
        py.detach(|| self.state.core(start, end, &rules))
            .map_err(PyValueError::new_err)
    }
}
