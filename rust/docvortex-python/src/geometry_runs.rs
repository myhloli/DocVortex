//! 从内部样本一次读取自有数值，最终按原 run 顺序物化统计对象。
use super::geometry::plain_coordinates;
use docvortex_core::geometry_runs::{self, Sample};
use pyo3::{
    prelude::*,
    types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyTuple},
};
use std::collections::HashMap;

/// 仅接收标准内部样本；不支持的结构返回 None，计算和物化错误直接传播。
#[pyfunction]
pub(super) fn build_geometry_runs<'py>(
    py: Python<'py>,
    samples: &Bound<'py, PyList>,
    by_line: &Bound<'py, PyDict>,
    keys: &Bound<'py, PyList>,
    sample_type: &Bound<'py, PyAny>,
    run_type: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyDict>>> {
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
    let mut records = Vec::with_capacity(samples.len());
    for (index, sample) in samples.iter().enumerate() {
        if !sample.get_type().is(sample_type) {
            return Ok(None);
        }
        // 重复的对象身份无法唯一表达成员索引，保留参考路径。
        if positions.insert(sample.as_ptr() as usize, index).is_some() {
            return Ok(None);
        }
        let key = sample.getattr("run_key")?;
        let Some(run) = ids.get_item(key)? else {
            return Ok(None);
        };
        let source = sample.getattr("local_source_bbox")?;
        let tight = sample.getattr("local_tight_bbox")?;
        let origin = sample.getattr("local_origin")?;
        let (Some(Some(source)), Some(Some(tight)), Some(Some(origin))) = (
            plain_coordinates::<4>(&source),
            plain_coordinates::<4>(&tight),
            plain_coordinates::<2>(&origin),
        ) else {
            return Ok(None);
        };
        if !sample.getattr("position")?.is_exact_instance_of::<PyInt>()
            || !sample
                .getattr("font_size")?
                .is_exact_instance_of::<PyFloat>()
            || !sample
                .getattr("is_anchor")?
                .is_exact_instance_of::<PyBool>()
        {
            return Ok(None);
        }
        let Ok(position) = sample.getattr("position")?.extract::<i64>() else {
            return Ok(None);
        };
        let Ok(size) = sample.getattr("font_size")?.extract::<f64>() else {
            return Ok(None);
        };
        let Ok(anchor) = sample.getattr("is_anchor")?.extract::<bool>() else {
            return Ok(None);
        };
        records.push(Sample {
            run: run.extract()?,
            position,
            source,
            tight,
            origin,
            size,
            anchor,
        });
    }
    let mut lines = Vec::with_capacity(by_line.len());
    for (_, line) in by_line.iter() {
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
    let Some(runs) = py.detach(move || geometry_runs::build(records, lines, families)) else {
        return Ok(None);
    };
    let output = PyDict::new(py);
    for (index, run) in runs.into_iter().enumerate() {
        let key = keys.get_item(index)?;
        let object = run_type.call1((&key,))?;
        let members = PyList::empty(py);
        for index in run.members {
            members.append(samples.get_item(index)?)?;
        }
        object.setattr("samples", members)?;
        object.setattr("pair_ratios", run.ratios)?;
        object.setattr("pair_overlaps", run.overlaps)?;
        object.setattr("advances", run.advances)?;
        object.setattr("tight_left_bearings", run.bearings)?;
        object.setattr("median_advance", run.median_advance)?;
        object.setattr("median_tight_left_bearing", run.median_bearing)?;
        object.setattr("strong_x_bad", run.strong)?;
        object.setattr("sibling_x_bad", run.sibling)?;
        output.set_item(key, object)?;
    }
    Ok(Some(output))
}
