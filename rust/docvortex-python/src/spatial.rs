//! Spatial index binding of baseline candidates, neighbor rows and annotation geometries.

use docvortex_core::geometry::Box4;
use pyo3::prelude::*;

/// The call holds a read-only interval tree and does not cache the Python row object.
#[pyclass(frozen)]
pub(super) struct BaselineCandidates {
    index: docvortex_core::spatial::IntervalIndex,
}

#[pymethods]
impl BaselineCandidates {
    /// After verifying the interval, a pure numerical index is established, and incomplete grouping parameters are rejected.
    #[new]
    fn new(py: Python<'_>, bounds: Vec<(f64, f64)>, groups: Vec<usize>) -> PyResult<Self> {
        py.detach(move || docvortex_core::spatial::IntervalIndex::new(bounds, groups))
            .map(|index| Self { index })
            .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("invalid interval index"))
    }

    /// Query consecutive rows according to a fixed capacity, and the densest row will not cause a full page square memory.
    fn rows(&self, py: Python<'_>, start: usize, count: usize, budget: usize) -> Vec<Vec<usize>> {
        py.detach(|| self.index.rows(start, count.min(64), budget.min(8192)))
    }
}

/// Filter row geometry within a single call, retaining independent access paths to the original row box.
#[pyclass(frozen)]
pub(super) struct BaselineGeometryCandidates {
    index: docvortex_core::spatial::BaselineGeometry,
}

#[pymethods]
impl BaselineGeometryCandidates {
    /// Create a verified read-only index and reject unsupported transmission records.
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
    /// Return a limited batch of the current original row order, without saving the entire page of row pairs.
    fn rows(&self, py: Python<'_>, start: usize, count: usize, budget: usize) -> Vec<Vec<usize>> {
        py.detach(|| self.index.rows(start, count, budget))
    }
}

/// Holds a purely numeric snapshot of the corridor segment, without retaining the Python or PDF handles.
#[pyclass(frozen)]
pub(super) struct AnnotationGeometry {
    index: docvortex_core::annotation_geometry::AnnotationGeometry,
}

#[pymethods]
impl AnnotationGeometry {
    /// Fragment geometry is verified and packed once, and unknown inputs are handled by the Python reference implementation.
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
    /// Return the order-preserving source and coordinate index; illegal queries will not silently produce partial results.
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

/// After freezing a page of row-level values, calculate the upper and lower physical headroom in batches.
#[pyfunction]
pub(super) fn title_gaps(
    py: Python<'_>,
    records: Vec<(usize, Option<usize>, Box4, f64, bool)>,
) -> Option<Vec<(Option<f64>, Option<f64>)>> {
    py.detach(move || docvortex_core::spatial::title_gaps(records))
}

/// Return the adjacent row index for Python to append the original analysis objects in the original order.
#[pyfunction]
pub(super) fn line_neighbors(
    py: Python<'_>,
    records: Vec<(usize, Box4, f64, f64)>,
) -> Option<Vec<(Option<usize>, Option<usize>)>> {
    py.detach(move || docvortex_core::spatial::line_neighbors(records))
}
