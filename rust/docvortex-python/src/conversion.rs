//! The box reading and Python quad conversion shared by the binding module do not carry computational decisions.

use docvortex_core::geometry::Box4;
use pyo3::prelude::*;
use pyo3::types::PyList;

pub(super) type BoxTuple = (f64, f64, f64, f64);

/// In the Python binding layer, the box array is materialized into the original immutable four-tuple.
pub(super) fn box_tuple(b: Box4) -> BoxTuple {
    (b[0], b[1], b[2], b[3])
}

/// Ordinary numerical values are extracted directly; special iterable input still uses the original Python verification semantics.
pub(super) fn read_boxes(
    values: &Bound<'_, PyList>,
    fallback: &Bound<'_, PyAny>,
) -> PyResult<Vec<Option<Box4>>> {
    values
        .iter()
        .map(|v| match v.extract::<Option<Box4>>() {
            Ok(b) => Ok(b),
            Err(_) => fallback.call1((v,))?.extract::<Option<Box4>>(),
        })
        .collect()
}
