//! Read the own value from the internal sample once, and finally materialize the statistical object according to the original run sequence.
use super::geometry::{plain_coordinates, shared_coordinates};
use docvortex_core::geometry;
use docvortex_core::geometry_runs::{self, Sample};
use docvortex_core::geometry_style::{self, Metadata, Style};
use pyo3::{
    prelude::*,
    types::{PyBool, PyDict, PyFloat, PyInt, PyList, PySet, PyString, PyTuple},
};
use std::collections::HashMap;

/// Only standard internal samples are received; unsupported structures return None, computation and materialization errors are propagated directly.
#[pyfunction]
pub(super) fn build_geometry_runs<'py>(
    py: Python<'py>,
    samples: &Bound<'py, PyList>,
    by_line: &Bound<'py, PyDict>,
    keys: &Bound<'py, PyList>,
    sample_type: &Bound<'py, PyAny>,
    run_type: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyDict>>> {
    Ok(prepare(
        py,
        samples,
        by_line,
        keys,
        sample_type,
        run_type,
        None,
        None,
        None,
    )?
    .map(|v| v.0))
}

/// Merge run statistics, cross-page style determination and line-by-line font size calculation, and only export objects at the final boundary.
#[pyfunction(signature = (samples, by_line, keys, sample_type, run_type, line_type, page_sizes=None, bbox_type=None))]
pub(super) fn build_geometry_style<'py>(
    py: Python<'py>,
    samples: &Bound<'py, PyList>,
    by_line: &Bound<'py, PyDict>,
    keys: &Bound<'py, PyList>,
    sample_type: &Bound<'py, PyAny>,
    run_type: &Bound<'py, PyAny>,
    line_type: &Bound<'py, PyAny>,
    page_sizes: Option<&Bound<'py, PyList>>,
    bbox_type: Option<&Bound<'py, PyAny>>,
) -> PyResult<
    Option<(
        Bound<'py, PyDict>,
        Bound<'py, PySet>,
        Bound<'py, PyDict>,
        bool,
    )>,
> {
    prepare(
        py,
        samples,
        by_line,
        keys,
        sample_type,
        run_type,
        Some(line_type),
        page_sizes,
        bbox_type,
    )
}

/// Only built-in integers are read, out-of-bounds and custom conversions are explicitly returned to the reference implementation.
fn read_index(value: &Bound<'_, PyAny>) -> Option<i64> {
    value
        .is_exact_instance_of::<PyInt>()
        .then(|| value.extract::<i64>().ok())
        .flatten()
}

/// Verify the effective height and retain the Python operation path for extremely large integers and special values.
fn height(value: &Bound<'_, PyAny>) -> Option<f64> {
    let result = if value.is_exact_instance_of::<PyFloat>() {
        value.extract::<f64>().ok()?
    } else {
        let n = read_index(value)?;
        if !(-(1_i64 << 53)..=(1_i64 << 53)).contains(&n) {
            return None;
        }
        n as f64
    };
    result.is_finite().then_some(result)
}

/// Prepare values once and reuse them to multiple algorithm stages without re-reading character objects between stages.
fn prepare<'py>(
    py: Python<'py>,
    samples: &Bound<'py, PyList>,
    by_line: &Bound<'py, PyDict>,
    keys: &Bound<'py, PyList>,
    sample_type: &Bound<'py, PyAny>,
    run_type: &Bound<'py, PyAny>,
    line_type: Option<&Bound<'py, PyAny>>,
    page_sizes: Option<&Bound<'py, PyList>>,
    bbox_type: Option<&Bound<'py, PyAny>>,
) -> PyResult<
    Option<(
        Bound<'py, PyDict>,
        Bound<'py, PySet>,
        Bound<'py, PyDict>,
        bool,
    )>,
> {
    // Attribute names and repeated fields are only prepared once, reducing the Python C API overhead of each sample.
    let attr_angle = pyo3::intern!(py, "angle");
    let attr_chars = pyo3::intern!(py, "chars");
    let attr_effective_height = pyo3::intern!(py, "effective_height");
    let attr_font_size = pyo3::intern!(py, "font_size");
    let attr_is_anchor = pyo3::intern!(py, "is_anchor");
    let attr_line = pyo3::intern!(py, "line");
    let attr_local_origin = pyo3::intern!(py, "local_origin");
    let attr_local_source_bbox = pyo3::intern!(py, "local_source_bbox");
    let attr_local_tight_bbox = pyo3::intern!(py, "local_tight_bbox");
    let attr_page_index = pyo3::intern!(py, "page_index");
    let attr_position = pyo3::intern!(py, "position");
    let attr_run_key = pyo3::intern!(py, "run_key");
    let attr_source_index = pyo3::intern!(py, "source_index");
    let ids = PyDict::new(py);
    let family_ids = PyDict::new(py);
    let mut families = Vec::with_capacity(keys.len());
    for (index, key) in keys.iter().enumerate() {
        if !key.is_exact_instance_of::<PyTuple>() || key.len()? != 6 {
            return Ok(None);
        }
        ids.set_item(&key, index)?;
        let family = key.get_item(0)?;
        let id = match family_ids.get_item(&family)? {
            Some(value) => value.extract::<usize>()?,
            None => {
                let id = family_ids.len();
                family_ids.set_item(&family, id)?;
                id
            }
        };
        families.push(id);
    }
    let mut positions = HashMap::new();
    let mut run_cache = HashMap::<usize, (Bound<'py, PyAny>, usize)>::new();
    let mut records = Vec::with_capacity(samples.len());
    let mut metadata = Vec::new();
    let mut line_cache = HashMap::<usize, (i64, f64)>::new();
    for (index, sample) in samples.iter().enumerate() {
        if !sample.get_type().is(sample_type) {
            return Ok(None);
        }
        // Duplicate object identities cannot uniquely express member indexes, and reference paths are retained.
        if positions.insert(sample.as_ptr() as usize, index).is_some() {
            return Ok(None);
        }
        let key = sample.getattr(attr_run_key)?;
        let identity = key.as_ptr() as usize;
        let run = if let Some((_, id)) = run_cache.get(&identity) {
            *id
        } else {
            let Some(run) = ids.get_item(&key)? else {
                return Ok(None);
            };
            let id = run.extract::<usize>()?;
            // A maximum of 4096 built-in immutable run key can be cached, and the custom hash still goes through the Python dictionary one by one.
            let plain = key.cast_exact::<PyTuple>().is_ok_and(|key| {
                key.iter().all(|v| {
                    v.is_exact_instance_of::<PyString>()
                        || v.is_exact_instance_of::<PyInt>()
                        || v.is_exact_instance_of::<PyFloat>()
                })
            });
            if plain && run_cache.len() < 4096 {
                run_cache.insert(identity, (key, id));
            }
            id
        };
        let source = sample.getattr(attr_local_source_bbox)?;
        let tight = sample.getattr(attr_local_tight_bbox)?;
        let origin = sample.getattr(attr_local_origin)?;
        let (Some(Some(source)), Some(Some(tight)), Some(Some(origin))) = (
            plain_coordinates::<4>(&source),
            plain_coordinates::<4>(&tight),
            plain_coordinates::<2>(&origin),
        ) else {
            return Ok(None);
        };
        let position_value = sample.getattr(attr_position)?;
        let size_value = sample.getattr(attr_font_size)?;
        let anchor_value = sample.getattr(attr_is_anchor)?;
        if !position_value.is_exact_instance_of::<PyInt>()
            || !size_value.is_exact_instance_of::<PyFloat>()
            || !anchor_value.is_exact_instance_of::<PyBool>()
        {
            return Ok(None);
        }
        let Ok(position) = position_value.extract::<i64>() else {
            return Ok(None);
        };
        let Ok(size) = size_value.extract::<f64>() else {
            return Ok(None);
        };
        let Ok(anchor) = anchor_value.extract::<bool>() else {
            return Ok(None);
        };
        if let Some(line_type) = line_type {
            let line = sample.getattr(attr_line)?;
            if !line.get_type().is(line_type) {
                return Ok(None);
            }
            let Some(page) = read_index(&sample.getattr(attr_page_index)?) else {
                return Ok(None);
            };
            let identity = line.as_ptr() as usize;
            let (source, height) = if let Some(values) = line_cache.get(&identity) {
                *values
            } else {
                let (Some(source), Some(height)) = (
                    read_index(&line.getattr(attr_source_index)?),
                    height(&line.getattr(attr_effective_height)?),
                ) else {
                    return Ok(None);
                };
                line_cache.insert(identity, (source, height));
                (source, height)
            };
            metadata.push(Metadata {
                page,
                source,
                height,
            });
        }
        records.push(Sample {
            run,
            position,
            source,
            tight,
            origin,
            size,
            anchor,
        });
    }
    let mut lines = Vec::with_capacity(by_line.len());
    let mut line_keys = Vec::with_capacity(by_line.len());
    for (key, line) in by_line.iter() {
        line_keys.push(key);
        let Ok(line) = line.cast_exact::<PyList>() else {
            return Ok(None);
        };
        let mut indices = Vec::with_capacity(line.len());
        for sample in line.iter() {
            let Some(index) = positions.get(&(sample.as_ptr() as usize)) else {
                return Ok(None);
            };
            indices.push(*index);
        }
        lines.push(indices);
    }
    let with_style = line_type.is_some();
    let Some((mut runs, style)) = py.detach(|| {
        let runs = geometry_runs::group(&records, &families)?;
        let style = if with_style {
            geometry_style::prepare(&records, &lines, &runs, &metadata)?
        } else {
            Style {
                inflated: Vec::new(),
                scales: Vec::new(),
            }
        };
        Some((runs, style))
    }) else {
        return Ok(None);
    };
    let restored = page_sizes.is_some() && !style.inflated.is_empty();
    let mut updates = Vec::new();
    let mut owners = Vec::new();
    if restored {
        let pages = page_sizes.unwrap();
        for (line_index, indices) in lines.iter().enumerate() {
            let key = &line_keys[line_index];
            if !key.is_exact_instance_of::<PyTuple>() || key.len()? != 2 {
                return Ok(None);
            }
            let Some(page) = read_index(&key.get_item(0)?) else {
                return Ok(None);
            };
            let page = if page < 0 {
                pages.len() as i64 + page
            } else {
                page
            };
            if page < 0 || page as usize >= pages.len() {
                return Ok(None);
            }
            let size = pages.get_item(page as usize)?;
            let Some(Some(size)) = plain_coordinates::<2>(&size) else {
                return Ok(None);
            };
            for index in indices {
                let sample = samples.get_item(*index)?;
                let line = sample.getattr(attr_line)?;
                let angle = line.getattr(attr_angle)?;
                let Some(angle) = read_index(&angle).and_then(|v| i32::try_from(v).ok()) else {
                    return Ok(None);
                };
                let chars = line.getattr(attr_chars)?;
                let Ok(chars) = chars.cast_exact::<PyList>() else {
                    return Ok(None);
                };
                let position = records[*index].position;
                let position = if position < 0 {
                    chars.len() as i64 + position
                } else {
                    position
                };
                if position < 0 || position as usize >= chars.len() {
                    return Ok(None);
                }
                let char = chars.get_item(position as usize)?;
                let Ok(char) = char.cast_exact::<PyDict>() else {
                    return Ok(None);
                };
                let raw = char
                    .get_item(pyo3::intern!(py, "bbox"))?
                    .unwrap_or_else(|| py.None().into_bound(py));
                let raw = if bbox_type.is_some_and(|ty| raw.get_type().is(ty)) {
                    raw.getattr(pyo3::intern!(py, "bbox"))?
                } else {
                    raw
                };
                let Some(raw_values) = plain_coordinates::<4>(&raw) else {
                    return Ok(None);
                };
                updates.push((*index, raw_values, size, angle));
                owners.push(raw);
            }
        }
    }
    let Some(repairs) = py.detach(|| {
        let mut repairs = Vec::with_capacity(updates.len());
        for (index, raw, size, angle) in updates {
            let repair = geometry::clip(geometry::normalize(raw, false), size).map(|source| {
                let local = geometry::rotate(source, size, angle);
                records[index].source = local;
                (index, source, local, angle)
            });
            repairs.push(repair);
        }
        geometry_runs::complete(&records, &lines, &families, &mut runs)?;
        Some(repairs)
    }) else {
        return Ok(None);
    };
    // The sample is written back only after all inputs and calculations are successful, and the semi-recovery state will not be left when the input is rejected.
    for (repair, raw) in repairs.into_iter().zip(owners) {
        if let Some((index, source, local, angle)) = repair {
            let sample = samples.get_item(index)?;
            let source = shared_coordinates(py, source, &[raw])?;
            let local = if angle == 0 {
                source.clone()
            } else {
                shared_coordinates(py, local, &[])?
            };
            sample.setattr(pyo3::intern!(py, "source_bbox"), source)?;
            sample.setattr(pyo3::intern!(py, "local_source_bbox"), local)?;
        }
    }
    let inflated = PySet::empty(py)?;
    let mut inflated_flags = vec![false; runs.len()];
    for index in style.inflated {
        inflated.add(keys.get_item(index)?)?;
        inflated_flags[index] = true;
    }
    let scales = PyDict::new(py);
    for (index, scale) in style.scales {
        scales.set_item(&line_keys[index], scale)?;
    }
    let output = materialize_runs(py, samples, keys, runs, &inflated_flags, run_type)?;
    Ok(Some((output, inflated, scales, restored)))
}

/// Compatible objects are generated for the final run and members remain references to the same batch of Python samples.
pub(super) fn materialize_runs<'py>(
    py: Python<'py>,
    samples: &Bound<'py, PyList>,
    keys: &Bound<'py, PyList>,
    runs: Vec<geometry_runs::Run>,
    inflated_flags: &[bool],
    run_type: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let output = PyDict::new(py);
    for (index, run) in runs.into_iter().enumerate() {
        let key = keys.get_item(index)?;
        let object = run_type.call1((&key,))?;
        let members = PyList::empty(py);
        for index in run.members {
            members.append(samples.get_item(index)?)?;
        }
        object.setattr(pyo3::intern!(py, "samples"), members)?;
        object.setattr(pyo3::intern!(py, "pair_ratios"), run.ratios)?;
        object.setattr(pyo3::intern!(py, "pair_overlaps"), run.overlaps)?;
        object.setattr(pyo3::intern!(py, "advances"), run.advances)?;
        object.setattr(pyo3::intern!(py, "tight_left_bearings"), run.bearings)?;
        object.setattr(pyo3::intern!(py, "median_advance"), run.median_advance)?;
        object.setattr(
            pyo3::intern!(py, "median_tight_left_bearing"),
            run.median_bearing,
        )?;
        object.setattr(pyo3::intern!(py, "strong_x_bad"), run.strong)?;
        object.setattr(pyo3::intern!(py, "sibling_x_bad"), run.sibling)?;
        if inflated_flags[index] {
            object.setattr(pyo3::intern!(py, "style_y_bad"), true)?;
        }
        output.set_item(key, object)?;
    }
    Ok(output)
}
