//! Ordered clustering, batch statistical binding of fonts and column spacing.

use docvortex_core::geometry::Box4;
use pyo3::prelude::*;

/// Batch clustering only returns indexes to avoid duplicating coordinates and public objects for cluster members.
#[pyfunction]
pub(super) fn ordered_clusters(
    py: Python<'_>,
    values: Vec<f64>,
    tolerance: f64,
    relative: f64,
    last_only: bool,
) -> Option<Vec<Vec<usize>>> {
    py.detach(move || {
        docvortex_core::statistics::ordered_clusters(values, tolerance, relative, last_only)
    })
}

/// Aggregate the verified local boxes, return the font size, main font index and line start characteristics, and do not return repeated character records.
#[pyfunction]
pub(super) fn typography_metrics(
    py: Python<'_>,
    boxes: Vec<Option<Box4>>,
    fonts: Vec<Option<usize>>,
    weights: Vec<Option<f64>>,
    families: Vec<Option<usize>>,
    fallback_height: f64,
) -> Option<docvortex_core::statistics::Typography> {
    py.detach(move || {
        docvortex_core::statistics::typography(boxes, fonts, weights, &families, fallback_height)
    })
}

/// The stable snapshot is consumed once within the band call, and the side effects of Python member sorting are retained.
#[pyfunction]
pub(super) fn lane_gap(
    py: Python<'_>,
    boxes: Vec<Box4>,
    heights: Vec<f64>,
    restored: Vec<bool>,
    skip: Vec<bool>,
) -> Option<(f64, f64)> {
    py.detach(move || docvortex_core::statistics::lane_gap(boxes, heights, restored, skip))
}
