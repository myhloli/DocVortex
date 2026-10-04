//! Superscript and subscript classification and pairing binding, Python continues to be responsible for text interpretation and materialization.

use docvortex_core::geometry::{self, Size};
use pyo3::prelude::*;
use pyo3::types::{PyFloat, PyList, PyTuple};

use super::conversion::read_boxes;

/// Batch calculation of intra-line superscript and subscript bidirectional nearest pairing, Python continues to be responsible for recursive materialization.
#[pyfunction]
pub(super) fn inline_script_matches(
    py: Python<'_>,
    records: Vec<docvortex_core::inline_pairs::Record>,
) -> Option<Vec<(usize, usize, bool, bool)>> {
    py.detach(|| docvortex_core::inline_pairs::matches(records))
}

/// Extract the materialized numerical features at one time, and then calculate the entire character role after releasing GIL.
#[pyfunction]
pub(super) fn script_roles(
    py: Python<'_>,
    records: Vec<docvortex_core::scripts::Record>,
) -> Option<Vec<u8>> {
    py.detach(move || docvortex_core::scripts::classify(records))
}

/// Merge verification and classification boundaries to avoid creating two intermediate Python coordinate objects for each character.
#[pyfunction]
pub(super) fn script_roles_raw(
    py: Python<'_>,
    loose: &Bound<'_, PyList>,
    tight: &Bound<'_, PyList>,
    origins: Vec<Option<Size>>,
    flags: Vec<u32>,
    fonts: Vec<i64>,
    fallback: &Bound<'_, PyAny>,
) -> PyResult<Option<Vec<u8>>> {
    let loose = read_boxes(loose, fallback)?;
    let tight = read_boxes(tight, fallback)?;
    if [tight.len(), origins.len(), flags.len(), fonts.len()]
        .iter()
        .any(|n| *n != loose.len())
    {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "script batch lengths differ",
        ));
    }
    Ok(py.detach(move || {
        let records = loose
            .into_iter()
            .zip(tight)
            .zip(origins)
            .zip(flags)
            .zip(fonts)
            .map(|((((l, t), o), mut flag), font)| {
                let l = geometry::normalize(l, true).unwrap_or([0.0; 4]);
                let t = geometry::normalize(t, true);
                let o = o.filter(|p| p.iter().all(|v| v.is_finite()));
                if flag & 3 == 0 && t.is_some() && o.is_some() {
                    flag |= 256;
                }
                (l, t, o, flag, font)
            })
            .collect();
        docvortex_core::scripts::classify(records)
    }))
}

/// The column input of independent formula segments is handed over to Rust once; each segment maintains the original classification boundary.
#[pyfunction]
pub(super) fn script_roles_raw_batch(
    py: Python<'_>,
    loose: &Bound<'_, PyList>,
    tight: &Bound<'_, PyList>,
    origins: Vec<Option<Size>>,
    flags: Vec<u32>,
    fonts: Vec<i64>,
    offsets: Vec<usize>,
    fallback: &Bound<'_, PyAny>,
) -> PyResult<Vec<Option<Vec<u8>>>> {
    let loose = read_boxes(loose, fallback)?;
    let tight = read_boxes(tight, fallback)?;
    let count = loose.len();
    if [tight.len(), origins.len(), flags.len(), fonts.len()]
        .iter()
        .any(|n| *n != count)
        || offsets.first() != Some(&0)
        || offsets.last() != Some(&count)
        || offsets.windows(2).any(|pair| pair[0] >= pair[1])
    {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "script batch lengths or offsets differ",
        ));
    }
    Ok(py.detach(move || {
        let records: Vec<_> = loose
            .into_iter()
            .zip(tight)
            .zip(origins)
            .zip(flags)
            .zip(fonts)
            .map(|((((l, t), o), mut flag), font)| {
                let l = geometry::normalize(l, true).unwrap_or([0.0; 4]);
                let t = geometry::normalize(t, true);
                let o = o.filter(|p| p.iter().all(|v| v.is_finite()));
                if flag & 3 == 0 && t.is_some() && o.is_some() {
                    flag |= 256;
                }
                (l, t, o, flag, font)
            })
            .collect();
        offsets
            .windows(2)
            .map(|pair| docvortex_core::scripts::classify(records[pair[0]..pair[1]].to_vec()))
            .collect()
    }))
}

/// Only the built-in floating point geometry of the native extraction layer is accepted, and the special Python object intersection restores the progressive path.
#[pyfunction]
pub(super) fn script_roles_plain_batch(
    py: Python<'_>,
    loose: &Bound<'_, PyList>,
    tight: &Bound<'_, PyList>,
    origins: &Bound<'_, PyList>,
    flags: Vec<u32>,
    fonts: Vec<i64>,
    offsets: Vec<usize>,
    fallback: &Bound<'_, PyAny>,
) -> PyResult<Option<Vec<Option<Vec<u8>>>>> {
    if !plain_float_records(loose, 4)?
        || !plain_float_records(tight, 4)?
        || !plain_float_records(origins, 2)?
    {
        return Ok(None);
    }
    let points = origins.extract::<Vec<Option<Size>>>()?;
    Ok(Some(script_roles_raw_batch(
        py, loose, tight, points, flags, fonts, offsets, fallback,
    )?))
}

/// Validate columnar records for true Python types and finite numeric values, without implicit conversion of integers or custom objects.
fn plain_float_records(values: &Bound<'_, PyList>, width: usize) -> PyResult<bool> {
    let py = values.py();
    for item in values.iter() {
        if item.is_none() {
            continue;
        }
        let kind = item.get_type();
        if !(kind.is(py.get_type::<PyList>()) || kind.is(py.get_type::<PyTuple>()))
            || item.len()? != width
        {
            return Ok(false);
        }
        for value in item.try_iter()? {
            let value = value?;
            if !value.get_type().is(py.get_type::<PyFloat>())
                || !value.extract::<f64>()?.is_finite()
            {
                return Ok(false);
            }
        }
    }
    Ok(true)
}
