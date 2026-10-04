//! Thin binding of table candidate continuation calculations, materializing integer members with source indices only at result boundaries.
use docvortex_core::table_candidates::RuleCandidates;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

#[pyclass(frozen)]
pub(super) struct PreparedRuleCandidates {
    state: RuleCandidates,
}

#[pymethods]
impl PreparedRuleCandidates {
    /// Python Immutable corridor data is created once after admission verification, and calculation errors are propagated explicitly.
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
    /// Retain core members in Rust and do not create the Python set in advance for candidate annotation and merging.
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
    /// A native call simultaneously completes the closed interval allocation and the first interval header exception determination.
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
    /// Complete member deduplication and core geometry expansion on the prepared corridor, and return to the original object source index.
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
