//! Document Risk Accumulator Binding: Only full line input touches final conclusion boundary Python.
use docvortex_core::geometry_risk::{Entry, Risk, RunKey};
use pyo3::prelude::*;
use std::collections::HashMap;

#[pyclass(module = "docvortex._native")]
pub(super) struct NativeGeometryRuns {
    values: HashMap<RunKey, usize>,
}

#[pymethods]
impl NativeGeometryRuns {
    /// Create a run numbering table for full-text sharing to ensure that the same key continues to accumulate across pages.
    #[new]
    fn new() -> Self {
        Self {
            values: HashMap::new(),
        }
    }

    /// Reports the current distinct run number, for testing and diagnostic purposes only.
    fn __len__(&self) -> usize {
        self.values.len()
    }
}

impl NativeGeometryRuns {
    /// Assign stable numbers to exactly the same font/angle/text key.
    pub(super) fn intern(&mut self, key: RunKey) -> usize {
        let next = self.values.len();
        *self.values.entry(key).or_insert(next)
    }
}

#[pyclass(module = "docvortex._native")]
pub(super) struct NativeGeometryRisk {
    state: Risk,
}

#[pymethods]
impl NativeGeometryRisk {
    /// Create a document status that only has Rust numerical statistics.
    #[new]
    fn new() -> Self {
        Self {
            state: Risk::default(),
        }
    }

    /// Consume one row after releasing GIL; None clearly indicates that the input does not belong to the finite standard numerical domain.
    fn add_line(
        &mut self,
        py: Python<'_>,
        page: usize,
        source: i64,
        height: f64,
        skip_y: bool,
        entries: Vec<Entry>,
    ) -> Option<bool> {
        py.detach(|| self.state.add_line(page, source, height, skip_y, entries))
    }

    /// Provide page-level self-owned evidence to continuously accumulate risks after completing run numbering within Rust.
    pub(super) fn add_prepared(
        &mut self,
        py: Python<'_>,
        page: usize,
        source: i64,
        height: f64,
        skip_y: bool,
        entries: Vec<Entry>,
    ) -> Option<bool> {
        py.detach(|| self.state.add_line(page, source, height, skip_y, entries))
    }

    /// Return layout and style risks without materializing character or font dictionaries.
    fn finish(&self, py: Python<'_>) -> (bool, bool) {
        py.detach(|| self.state.finish())
    }
}
