//! Repeated drawing, batch binding of hidden text candidates and mapping groups.

use docvortex_core::dedup;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple, PyType};
use std::collections::{HashMap, HashSet};

/// Ordinary integers use bounded reads, oversized or custom integers are handled by the Python reference implementation.
fn plain_integer(value: &Bound<'_, PyAny>) -> Option<i64> {
    value
        .is_exact_instance_of::<PyInt>()
        .then(|| value.extract::<i64>().ok())
        .flatten()
}

/// Directly borrow the existing character list, prepare the mapping protection interval and the Glyph line box once, and do not repackage the old grouping kernel.
#[pyfunction]
pub(super) fn mapping_glyph_rows<'py>(
    py: Python<'py>,
    chars: &Bound<'py, PyList>,
    bbox_type: &Bound<'py, PyType>,
) -> PyResult<Option<Vec<(usize, usize, Option<Bound<'py, PyTuple>>, f64)>>> {
    let mut fonts = HashSet::new();
    let mut text_flags = HashMap::new();
    let mut output: Vec<(usize, usize, Option<Bound<'py, PyTuple>>, f64)> = Vec::new();
    let mut previous: Option<(
        Bound<'py, PyAny>,
        i64,
        Bound<'py, PyDict>,
        f64,
        Option<[f64; 2]>,
        [f64; 4],
        bool,
    )> = None;
    for (position, item) in chars.iter().enumerate() {
        if !item.is_exact_instance_of::<PyDict>() {
            return Ok(None);
        }
        let char = item.cast::<PyDict>()?;
        if char
            .iter()
            .any(|(key, _value)| !key.is_exact_instance_of::<PyString>())
        {
            return Ok(None);
        }
        let (Some(text), Some(index), Some(font), Some(raw), Some(rotation)) = (
            char.get_item(pyo3::intern!(py, "char"))?,
            char.get_item(pyo3::intern!(py, "char_idx"))?,
            char.get_item(pyo3::intern!(py, "font"))?,
            char.get_item(pyo3::intern!(py, "bbox"))?,
            char.get_item(pyo3::intern!(py, "rotation"))?,
        ) else {
            return Ok(None);
        };
        if !text.is_exact_instance_of::<PyString>()
            || !font.is_exact_instance_of::<PyDict>()
            || !rotation.is_exact_instance_of::<PyFloat>()
        {
            return Ok(None);
        }
        let Some(index) = plain_integer(&index) else {
            return Ok(None);
        };
        let font = font.cast_into::<PyDict>()?;
        let font_id = font.as_ptr() as usize;
        if !fonts.contains(&font_id) {
            for (key, value) in font.iter() {
                if !key.is_exact_instance_of::<PyString>()
                    || !(value.is_none()
                        || value.is_exact_instance_of::<PyString>()
                        || value.is_exact_instance_of::<PyInt>()
                        || value.is_exact_instance_of::<PyFloat>()
                        || value.is_exact_instance_of::<PyBool>())
                {
                    return Ok(None);
                }
            }
            if fonts.len() >= 4096 {
                fonts.clear();
            }
            fonts.insert(font_id);
        }
        let raw = if raw.get_type().is(bbox_type) {
            raw.getattr(pyo3::intern!(py, "bbox"))?
        } else {
            raw
        };
        let Some(Some(box4)) = super::geometry::plain_coordinates::<4>(&raw) else {
            return Ok(None);
        };
        let origin = char
            .get_item(pyo3::intern!(py, "origin"))?
            .unwrap_or_else(|| py.None().into_bound(py));
        let Some(origin) = super::geometry::plain_coordinates::<2>(&origin) else {
            return Ok(None);
        };
        let object = char
            .get_item(pyo3::intern!(py, "text_object_id"))?
            .unwrap_or_else(|| py.None().into_bound(py));
        if !object.is_none() && !object.is_exact_instance_of::<PyInt>() {
            return Ok(None);
        }
        let rotation = rotation.extract::<f64>()?;
        let angle = if let Some(value) = char.get_item(pyo3::intern!(py, "writing_angle"))? {
            if !value.is_exact_instance_of::<PyFloat>() {
                return Ok(None);
            }
            value.extract::<f64>()?
        } else {
            -rotation
        };
        if !rotation.is_finite() || !angle.is_finite() {
            return Ok(None);
        }
        let last_source =
            if let Some(source) = char.get_item(pyo3::intern!(py, "source_indices"))? {
                if !source.is_exact_instance_of::<PyTuple>() || source.len()? == 0 {
                    return Ok(None);
                }
                let mut maximum = i64::MIN;
                for value in source.cast::<PyTuple>()?.iter() {
                    let Some(value) = plain_integer(&value) else {
                        return Ok(None);
                    };
                    maximum = maximum.max(value);
                }
                maximum
            } else {
                index
            };
        let Ok(text_key) = text.cast::<PyString>()?.to_str() else {
            return Ok(None);
        };
        let has_text = if let Some(value) = text_flags.get(text_key) {
            *value
        } else {
            let value = text.call_method0(pyo3::intern!(py, "strip"))?.is_truthy()?;
            if text_key.len() <= 32 {
                if text_flags.len() >= 4096 {
                    text_flags.clear();
                }
                text_flags.insert(text_key.to_owned(), value);
            }
            value
        };
        let same = if let Some((
            obj,
            maximum,
            previous_font,
            previous_rotation,
            previous_origin,
            previous_box,
            previous_text,
        )) = &previous
        {
            !obj.is_none()
                && obj.eq(&object)?
                && maximum.checked_add(1) == Some(index)
                && previous_font.eq(&font)?
                && *previous_rotation == rotation
                && previous_origin
                    .zip(origin)
                    .is_some_and(|(a, b)| a.iter().zip(b).all(|(x, y)| (x - y).abs() <= 0.001))
                && previous_box
                    .iter()
                    .zip(box4)
                    .all(|(x, y)| (x - y).abs() <= 0.001)
                && *previous_text
                && has_text
        } else {
            false
        };
        if same {
            // The heterogeneous character protection group also returns the complete interval; Unicode conversion is executed by Python after all preparations are completed.
            output.last_mut().expect("mapping group exists").1 = position + 1;
        } else {
            let box_tuple = if box4[2] > box4[0] && box4[3] > box4[1] {
                Some(PyTuple::new(
                    py,
                    (0..4)
                        .map(|i| raw.get_item(i))
                        .collect::<PyResult<Vec<_>>>()?,
                )?)
            } else {
                None
            };
            output.push((position, position + 1, box_tuple, angle));
        }
        previous = Some((object, last_source, font, rotation, origin, box4, has_text));
    }
    Ok(Some(output))
}

/// Generate repeated drawing candidates in batches without calling back Python in the inner loop.
#[pyfunction]
pub(super) fn paint_pairs(
    py: Python<'_>,
    records: Vec<dedup::PaintRecord>,
) -> Option<(Vec<dedup::Pair>, Vec<dedup::Offset>)> {
    py.detach(move || dedup::paint_pairs(records))
}

/// After checking the source index, calculate the earliest source connectivity relationship in batches.
#[pyfunction]
pub(super) fn dedup_components(
    py: Python<'_>,
    count: usize,
    pairs: Vec<dedup::Pair>,
) -> PyResult<Vec<usize>> {
    if pairs.iter().any(|(a, b)| *a >= count || *b >= count) {
        return Err(pyo3::exceptions::PyIndexError::new_err(
            "glyph index out of range",
        ));
    }
    Ok(py.detach(move || dedup::components(count, &pairs)))
}

/// Return the geometry candidate for hidden OCR, text comparison and source materialization are still owned by Python.
#[pyfunction]
pub(super) fn hidden_candidates(
    py: Python<'_>,
    records: Vec<dedup::HiddenRecord>,
) -> Option<Vec<(Vec<usize>, Vec<usize>)>> {
    py.detach(move || dedup::hidden_candidates(records))
}

/// Confirm the translation evidence under the original candidate order and verify all source indexes.
#[pyfunction]
pub(super) fn confirmed_offsets(
    py: Python<'_>,
    records: Vec<dedup::EvidenceRecord>,
    pairs: Vec<dedup::Offset>,
    exact: Vec<dedup::Pair>,
) -> PyResult<Vec<dedup::Pair>> {
    if pairs
        .iter()
        .any(|p| p.0 >= records.len() || p.1 >= records.len())
        || exact
            .iter()
            .any(|(a, b)| *a >= records.len() || *b >= records.len())
    {
        return Err(pyo3::exceptions::PyIndexError::new_err(
            "glyph index out of range",
        ));
    }
    Ok(py.detach(move || dedup::confirmed_offsets(records, pairs, exact)))
}

/// Only pass each character value once, and return the grouping range to reuse the original character object.
#[pyfunction]
pub(super) fn mapping_runs(
    py: Python<'_>,
    records: Vec<dedup::MappingRecord>,
) -> Option<Vec<(usize, usize)>> {
    py.detach(move || dedup::mapping_runs(records))
}
