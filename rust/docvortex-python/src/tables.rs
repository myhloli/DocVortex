//! Table calculations are bound to Python within the call to the index state.

use docvortex_core::geometry::Box4;
use docvortex_core::tables;
use pyo3::prelude::*;
use pyo3::types::PyList;

use super::conversion::{box_tuple, read_boxes, BoxTuple};

/// Row range extreme value index, do not return repeated floating point coordinates to Python.
#[pyclass(frozen)]
pub(super) struct TableRowGeometry {
    index: docvortex_core::row_geometry::RowGeometry,
}

#[pymethods]
impl TableRowGeometry {
    /// All limited frames are verified at one time during construction, and illegal input must be returned to the reference implementation.
    #[new]
    fn new(py: Python<'_>, boxes: Vec<[f64; 4]>) -> PyResult<Self> {
        py.detach(|| docvortex_core::row_geometry::RowGeometry::new(boxes))
            .map(|index| Self { index })
            .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("nonfinite row geometry"))
    }

    /// Return which row the four coordinates come from, and Python reuses existing coordinate objects.
    fn union_indices(&self, py: Python<'_>, start: usize, end: usize) -> Option<[usize; 4]> {
        py.detach(|| self.index.union_indices(start, end))
    }

    /// The original row order is retained, and only the prefixes that must not exceed the bottom of the table are skipped at the lower boundary.
    fn first_after(&self, py: Python<'_>, bottom: f64) -> Option<usize> {
        if !bottom.is_finite() {
            return None;
        }
        Some(py.detach(|| self.index.first_after(bottom)))
    }
}

/// The stable column accumulation status within the call is read, and the mean value is read without folding the compensation amount.
#[pyclass]
pub(super) struct StableColumnClusters {
    state: docvortex_core::columns::Columns,
}

#[pymethods]
impl StableColumnClusters {
    /// Python Bounds selects the validated floating point summation mode according to the interpreter version.
    #[new]
    fn new(compensated: bool) -> Self {
        Self {
            state: docvortex_core::columns::Columns::new(compensated),
        }
    }

    /// Only new rows after the strict prefix are passed in, and GIL is released during pure numerical calculations.
    fn extend(
        &mut self,
        py: Python<'_>,
        rows: Vec<Vec<(f64, f64)>>,
        tolerance: f64,
    ) -> Option<(usize, f64)> {
        py.detach(|| self.state.extend(&rows, tolerance))
    }

    /// Return the mean, cumulative results, number of members, and number of covered rows for differential testing without exposing the Python object.
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

/// The text height after sorting is reused for the whole page candidate, and only the interval and core source are passed in to the query.
#[pyclass(frozen)]
pub(super) struct TableNoteMetrics {
    metrics: std::sync::Arc<docvortex_core::statistics::NoteMetrics>,
}

/// The source members of the verified corridor row and their center range are only reused in the current candidate group.
#[pyclass(frozen)]
pub(super) struct TableNoteCore {
    rows: docvortex_core::note_index::CoreRows,
    owner: std::sync::Arc<docvortex_core::statistics::NoteMetrics>,
}

#[pymethods]
impl TableNoteMetrics {
    /// All corridor row members are received at one time, and subsequent candidates are only passed into row intervals.
    fn prepare_rows(&self, py: Python<'_>, members: Vec<Vec<i64>>) -> TableNoteCore {
        TableNoteCore {
            rows: py.detach(|| self.metrics.rows(members)),
            owner: self.metrics.clone(),
        }
    }

    /// Skip Python member verification and packaging, and when the index is not applicable, the source is still accurately excluded by Rust.
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
    /// Reject non-finite numeric values, ensuring that the reference implementation takes care of special ordering semantics.
    #[new]
    fn new(py: Python<'_>, items: Vec<(i64, f64, f64)>) -> PyResult<Self> {
        py.detach(move || docvortex_core::statistics::NoteMetrics::new(items))
            .map(|metrics| Self {
                metrics: std::sync::Arc::new(metrics),
            })
            .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("invalid note metrics"))
    }

    /// GIL is released during numerical filtering, and the limited input overflow returns to the reference path flag.
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

/// The table group row only returns the member index, and the original glyph object is reused by Python.
#[pyfunction]
pub(super) fn table_visual_rows(
    py: Python<'_>,
    boxes: Vec<Box4>,
    ids: Vec<usize>,
    median_height: f64,
) -> Option<Vec<Vec<usize>>> {
    py.detach(move || tables::visual_rows(boxes, ids, median_height))
}

/// The materialized character centers are counted in batches according to the entire table track, without changing the number of candidates.
#[pyfunction]
pub(super) fn table_row_occupancy(
    py: Python<'_>,
    rows: Vec<Vec<f64>>,
    tracks: Vec<f64>,
) -> Option<Vec<Vec<usize>>> {
    py.detach(move || tables::row_occupancy(rows, tracks))
}

/// The table area filters the entire page of characters at a time, and special input continues to be processed along the original verification entry of Python.
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

/// Query and calculate coverage for the entire batch of tracks to avoid binding calls on a segment-by-line basis.
#[pyfunction]
pub(super) fn coverage_batch(
    py: Python<'_>,
    rules: Vec<tables::Rule>,
    queries: Vec<tables::Query>,
) -> Option<Vec<f64>> {
    py.detach(move || tables::coverage_batch(rules, queries))
}

/// Merge adjacent line segments in batches according to Python cluster coordinates.
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

/// Complete cell allocation of all characters in batches, retaining index and ambiguity bits.
#[pyfunction]
pub(super) fn assign_cells(
    py: Python<'_>,
    glyphs: Vec<Box4>,
    specs: Vec<Box4>,
    index: Option<tables::GridIndex>,
) -> Option<Vec<(Option<usize>, bool)>> {
    py.detach(move || tables::assign_cells(glyphs, specs, index))
}

/// Grid join is performed once after verifying the atomic lattice index.
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

/// Convert the legal union set into a rectangular cell, and return the parent node after path compression at the same time.
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
    // The source is private Python and is searched; preventing abnormal loops at the binding boundary from causing a native infinite loop.
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
