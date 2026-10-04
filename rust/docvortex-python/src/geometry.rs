//! Character geometry is bound to coordinate transformation, retaining the Python coordinate object reuse semantics.

use docvortex_core::extraction;
use docvortex_core::geometry::{self, Box4, Size};
use pyo3::prelude::*;
use pyo3::types::{PyFloat, PyInt, PyList, PyTuple};

use super::conversion::{box_tuple, read_boxes, BoxTuple};

/// After the PDFium reading is completed, the values are converted in batches without touching the handle or synchronization lock.
#[pyfunction]
pub(super) fn materialize_geometry(
    py: Python<'_>,
    rows: Vec<extraction::RawGeometry>,
    frame: Box4,
    rounded: Size,
    angle: i32,
) -> Vec<(Box4, Option<BoxTuple>, Option<BoxTuple>, Option<(f64, f64)>)> {
    py.detach(move || {
        extraction::materialize(rows, frame, rounded, angle)
            .into_iter()
            .map(|(layout, loose, tight, origin)| {
                (
                    layout,
                    loose.map(box_tuple),
                    tight.map(box_tuple),
                    origin.map(|p| (p[0], p[1])),
                )
            })
            .collect()
    })
}

/// The entire row of anchor points is consumed at one time, repeated coordinates are not returned, and only the statistics of qualified adjacent pairs are returned.
#[pyfunction]
pub(super) fn anchor_pairs(
    py: Python<'_>,
    records: Vec<(usize, Box4, Box4, Size, f64)>,
    positive_source: bool,
) -> Option<Vec<(usize, f64, f64, bool)>> {
    py.detach(move || geometry::anchor_pairs(records, positive_source))
}

/// Batch replacement for frame-by-frame conversion, only holding Python objects at the boundary.
#[pyfunction]
pub(super) fn normalize_boxes(
    py: Python<'_>,
    values: &Bound<'_, PyList>,
    strict: bool,
    fallback: &Bound<'_, PyAny>,
) -> PyResult<Vec<Option<BoxTuple>>> {
    let values = read_boxes(values, fallback)?;
    Ok(py.detach(move || {
        values
            .into_iter()
            .map(|b| geometry::normalize(b, strict).map(box_tuple))
            .collect()
    }))
}

/// Complete line cropping, rotation, kerning statistics and grouping at one time; the result retains the source range.
#[pyfunction]
pub(super) fn visual_runs(
    py: Python<'_>,
    raw: &Bound<'_, PyList>,
    overrides: &Bound<'_, PyList>,
    flags: Vec<u8>,
    size: Size,
    angle: i32,
    fallback: &Bound<'_, PyAny>,
) -> PyResult<Vec<(usize, usize, Option<BoxTuple>)>> {
    let raw = read_boxes(raw, fallback)?;
    let overrides = read_boxes(overrides, fallback)?;
    if raw.len() != overrides.len() || raw.len() != flags.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "geometry batch lengths differ",
        ));
    }
    Ok(py.detach(move || {
        geometry::visual_runs(raw, overrides, flags, size, angle)
            .into_iter()
            .map(|(a, b, c)| (a, b, c.map(box_tuple)))
            .collect()
    }))
}

/// Calculate the legal local frame required for font statistics once and keep the original index corresponding to the empty character.
#[pyfunction]
pub(super) fn local_boxes(
    py: Python<'_>,
    values: &Bound<'_, PyList>,
    size: Size,
    angle: i32,
    fallback: &Bound<'_, PyAny>,
) -> PyResult<Vec<Option<BoxTuple>>> {
    let values = read_boxes(values, fallback)?;
    Ok(py.detach(move || {
        values
            .into_iter()
            .map(|b| {
                geometry::clip(geometry::normalize(b, false), size)
                    .map(|b| box_tuple(geometry::rotate(b, size, angle)))
            })
            .collect()
    }))
}

/// Reuse existing Python floating point objects for unchanged coordinates to avoid duplicating the entire coordinates of the canonical sample.
pub(super) fn shared_coordinates<'py, const N: usize>(
    py: Python<'py>,
    values: [f64; N],
    candidates: &[Bound<'py, PyAny>],
) -> PyResult<Bound<'py, PyAny>> {
    let mut output = Vec::with_capacity(N);
    for (index, value) in values.into_iter().enumerate() {
        let mut shared = None;
        for candidate in candidates {
            // Only ordinary transport containers are read; special input has been processed by the original validator and the user method is not triggered again.
            if !candidate.is_exact_instance_of::<PyList>()
                && !candidate.is_exact_instance_of::<PyTuple>()
            {
                continue;
            }
            if let Ok(item) = candidate.get_item(index) {
                if item.is_exact_instance_of::<PyFloat>()
                    && item.extract::<f64>()?.to_bits() == value.to_bits()
                {
                    shared = Some(item);
                    break;
                }
            }
        }
        output.push(shared.unwrap_or_else(|| PyFloat::new(py, value).into_any()));
    }
    Ok(PyTuple::new(py, output)?.into_any())
}

/// Numerical values are still calculated in batches, but the unrotated local frames reuse source/tight and tuple, and long-term surviving coordinates are not copied.
#[pyfunction]
pub(super) fn source_rows<'py>(
    py: Python<'py>,
    raw: &Bound<'py, PyList>,
    side: &Bound<'py, PyList>,
    tight: &Bound<'py, PyList>,
    origins: &Bound<'py, PyList>,
    rotations: Vec<f64>,
    size: Size,
    angle: i32,
    fallback: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyList>> {
    let raw_values = read_boxes(raw, fallback)?;
    let side_values = read_boxes(side, fallback)?;
    let tight_values = read_boxes(tight, fallback)?;
    let origin_values = origins.extract::<Vec<Option<Size>>>()?;
    if [
        side_values.len(),
        tight_values.len(),
        origin_values.len(),
        rotations.len(),
    ]
    .iter()
    .any(|n| *n != raw_values.len())
    {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "geometry batch lengths differ",
        ));
    }
    let rows = raw_values
        .into_iter()
        .zip(side_values)
        .zip(tight_values)
        .zip(origin_values)
        .zip(rotations)
        .map(|((((r, s), t), o), a)| (r, s, t, o, a))
        .collect();
    let prepared = py.detach(move || geometry::source_rows(rows, size, angle));
    let output = PyList::empty(py);
    for (index, row) in prepared.into_iter().enumerate() {
        if let Some((s, t, o, ls, lt, lo)) = row {
            let source = shared_coordinates(py, s, &[raw.get_item(index)?, side.get_item(index)?])?;
            let tight_box = shared_coordinates(py, t, &[tight.get_item(index)?])?;
            let origin = shared_coordinates(py, o, &[origins.get_item(index)?])?;
            let local_source = if angle == 0 {
                source.clone()
            } else {
                shared_coordinates(py, ls, &[])?
            };
            let local_tight = if angle == 0 {
                tight_box.clone()
            } else {
                shared_coordinates(py, lt, &[])?
            };
            let local_origin = if angle == 0 {
                origin.clone()
            } else {
                shared_coordinates(py, lo, &[])?
            };
            output.append(PyTuple::new(
                py,
                [
                    source,
                    tight_box,
                    origin,
                    local_source,
                    local_tight,
                    local_origin,
                ],
            )?)?;
        } else {
            output.append(py.None())?;
        }
    }
    Ok(output)
}

/// Only borrowing the built-in floating point sequence, special types and exception values return the reference path without executing the conversion callback.
pub(super) fn plain_coordinates<const N: usize>(
    value: &Bound<'_, PyAny>,
) -> Option<Option<[f64; N]>> {
    if value.is_none() {
        return Some(None);
    }
    if (!value.is_exact_instance_of::<PyList>() && !value.is_exact_instance_of::<PyTuple>())
        || value.len().ok()? != N
    {
        return None;
    }
    let mut result = [0.0; N];
    for (index, number) in result.iter_mut().enumerate() {
        let item = value.get_item(index).ok()?;
        if !item.is_exact_instance_of::<PyFloat>() {
            return None;
        }
        *number = item.extract::<f64>().ok()?;
        if !number.is_finite() {
            return None;
        }
    }
    Some(Some(result))
}

/// Each row receives only one order-preserving record, integrating ordinary value verification and existing source frame selection, cropping and rotation.
#[pyfunction]
pub(super) fn source_rows_plain<'py>(
    py: Python<'py>,
    records: &Bound<'py, PyList>,
    size: Size,
    angle: i32,
) -> PyResult<Option<Bound<'py, PyList>>> {
    let mut rows = Vec::with_capacity(records.len());
    let mut originals = Vec::with_capacity(records.len());
    for record in records.iter() {
        if !record.is_exact_instance_of::<PyTuple>() || record.len()? != 6 {
            return Ok(None);
        }
        let position = record.get_item(0)?;
        if !position.is_exact_instance_of::<PyInt>() || position.extract::<usize>().is_err() {
            return Ok(None);
        }
        let raw = record.get_item(1)?;
        let side = record.get_item(2)?;
        let tight = record.get_item(3)?;
        let origin = record.get_item(4)?;
        let rotation = record.get_item(5)?;
        let (Some(r), Some(s), Some(t), Some(o)) = (
            plain_coordinates::<4>(&raw),
            plain_coordinates::<4>(&side),
            plain_coordinates::<4>(&tight),
            plain_coordinates::<2>(&origin),
        ) else {
            return Ok(None);
        };
        if !rotation.is_exact_instance_of::<PyFloat>() {
            return Ok(None);
        }
        let rotation = rotation.extract::<f64>()?;
        if !rotation.is_finite() {
            return Ok(None);
        }
        rows.push((r, s, t, o, rotation));
        originals.push((position, raw, side, tight, origin));
    }
    let prepared = py.detach(move || geometry::source_rows(rows, size, angle));
    let output = PyList::empty(py);
    for ((position, raw, side, tight, origin), row) in originals.into_iter().zip(prepared) {
        if let Some((s, t, o, ls, lt, lo)) = row {
            let source = shared_coordinates(py, s, &[raw, side])?;
            let tight = shared_coordinates(py, t, &[tight])?;
            let origin = shared_coordinates(py, o, &[origin])?;
            let local_source = if angle == 0 {
                source.clone()
            } else {
                shared_coordinates(py, ls, &[])?
            };
            let local_tight = if angle == 0 {
                tight.clone()
            } else {
                shared_coordinates(py, lt, &[])?
            };
            let local_origin = if angle == 0 {
                origin.clone()
            } else {
                shared_coordinates(py, lo, &[])?
            };
            let values = PyTuple::new(
                py,
                [
                    source,
                    tight,
                    origin,
                    local_source,
                    local_tight,
                    local_origin,
                ],
            )?;
            output.append(PyTuple::new(py, [position, values.into_any()])?)?;
        }
    }
    Ok(Some(output))
}
