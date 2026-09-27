//! 从内部样本一次读取自有数值，最终按原 run 顺序物化统计对象。
use super::geometry::{plain_coordinates, shared_coordinates};
use docvortex_core::geometry;
use docvortex_core::geometry_runs::{self, Sample};
use docvortex_core::geometry_style::{self, Metadata, Style};
use pyo3::{
    prelude::*,
    types::{PyBool, PyDict, PyFloat, PyInt, PyList, PySet, PyString, PyTuple},
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

/// 合并 run 统计、跨页样式判定和逐行字号计算，仅在最终边界导出对象。
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

/// 只读取内置整数，越界和自定义转换明确交回参考实现。
fn read_index(value: &Bound<'_, PyAny>) -> Option<i64> {
    value
        .is_exact_instance_of::<PyInt>()
        .then(|| value.extract::<i64>().ok())
        .flatten()
}

/// 验证有效高度，保留超大整数和特殊数值的 Python 运算路径。
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

/// 一次准备数值并复用给多个算法阶段，不在阶段之间重新读取字符对象。
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
    // 属性名和重复字段只准备一次，减少每个样本的 Python C API 开销。
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
        // 重复的对象身份无法唯一表达成员索引，保留参考路径。
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
            // 最多缓存 4096 个内置不可变 run key，自定义哈希仍逐次走 Python 字典。
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
    // 所有输入和计算成功后才回写样本，拒绝输入时不会留下半恢复状态。
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

/// 为最终 run 生成兼容对象，成员保持为同一批 Python 样本的引用。
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
