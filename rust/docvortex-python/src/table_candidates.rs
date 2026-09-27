//! 表格候选连续计算的薄绑定，仅在结果边界物化整数成员与来源索引。
use docvortex_core::table_candidates::RuleCandidates;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

#[pyclass(frozen)]
pub(super) struct PreparedRuleCandidates {
    state: RuleCandidates,
}

#[pymethods]
impl PreparedRuleCandidates {
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
