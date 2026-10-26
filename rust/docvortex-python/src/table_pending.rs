//! 表格文字输入一次读取和物化，完整验证当前状态后才执行原文字规范化。
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use std::collections::HashMap;

/// 普通有限数值框保留原浮点转换，未知 Bbox 子类及大整数继续由 Python 处理。
fn bbox(value: &Bound<'_, PyAny>, bbox_type: &Bound<'_, PyAny>) -> PyResult<Option<[f64; 4]>> {
    let value = if value.get_type().is(bbox_type) {
        value.getattr(pyo3::intern!(value.py(), "bbox"))?
    } else {
        value.clone()
    };
    if (!value.is_exact_instance_of::<PyList>() && !value.is_exact_instance_of::<PyTuple>())
        || value.len()? != 4
    {
        return Ok(None);
    }
    let mut result = [0.0_f64; 4];
    for (i, number) in result.iter_mut().enumerate() {
        let item = value.get_item(i)?;
        if !item.is_exact_instance_of::<PyFloat>() && !item.is_exact_instance_of::<PyInt>() {
            return Ok(None);
        }
        let Ok(v) = item.extract::<f64>() else {
            return Ok(None);
        };
        if !v.is_finite() || v.abs() > 2f64.powi(50) {
            return Ok(None);
        }
        *number = v;
    }
    Ok(Some(result))
}

/// 只保留有限的普通基线原点；没有可用几何时不能为单元格伪造方向证据。
fn origin(value: Option<Bound<'_, PyAny>>, table: [f64; 4], angle: i32) -> Option<(f64, f64)> {
    let value = value?;
    if (!value.is_exact_instance_of::<PyList>() && !value.is_exact_instance_of::<PyTuple>())
        || value.len().ok()? != 2
    {
        return None;
    }
    let x = value.get_item(0).ok()?.extract::<f64>().ok()?;
    let y = value.get_item(1).ok()?.extract::<f64>().ok()?;
    if !x.is_finite() || !y.is_finite() {
        return None;
    }
    let (x, y) = (x - table[0], y - table[1]);
    let (width, height) = (table[2] - table[0], table[3] - table[1]);
    Some(match angle {
        90 => (y, width - x),
        180 => (width - x, height - y),
        270 => (height - y, x),
        _ => (x, y),
    })
}

/// 全部输入先核验；坐标筛选在自有缓冲上释放 GIL，输出沿用冻结 dataclass 的通用槽初始化。
#[pyfunction]
pub(super) fn table_pending_glyphs_owned<'py>(
    py: Python<'py>,
    chars: &Bound<'py, PyAny>,
    table: &Bound<'py, PyAny>,
    angle: &Bound<'py, PyAny>,
    bbox_type: &Bound<'py, PyAny>,
    normalizer: &Bound<'py, PyAny>,
    record_type: &Bound<'py, PyAny>,
    allocator: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyList>>> {
    if (!chars.is_exact_instance_of::<PyList>() && !chars.is_exact_instance_of::<PyTuple>())
        || !angle.is_exact_instance_of::<PyInt>()
    {
        return Ok(None);
    }
    let Ok(angle) = angle.extract::<i32>() else {
        return Ok(None);
    };
    let Some(table) = bbox(table, bbox_type)? else {
        return Ok(None);
    };
    let mut values = Vec::new();
    let mut advances = Vec::new();
    let mut source = Vec::new();
    for (i, item) in chars.try_iter()?.enumerate() {
        let item = item?;
        let Ok(item) = item.cast_exact::<PyDict>() else {
            return Ok(None);
        };
        let raw = item.get_item(pyo3::intern!(py, "bbox"))?;
        let b = if let Some(raw) = raw {
            if raw.is_none() {
                None
            } else {
                let Some(b) = bbox(&raw, bbox_type)? else {
                    return Ok(None);
                };
                Some(b)
            }
        } else {
            None
        };
        let index = item.get_item(pyo3::intern!(py, "char_idx"))?;
        let index = if let Some(index) = index {
            if !index.is_exact_instance_of::<PyInt>() {
                return Ok(None);
            }
            let Ok(value) = index.extract::<i64>() else {
                return Ok(None);
            };
            value
        } else {
            let Ok(value) = i64::try_from(i) else {
                return Ok(None);
            };
            value
        };
        let text = item.get_item(pyo3::intern!(py, "char"))?;
        let text = if let Some(text) = text.filter(|v| !v.is_none()) {
            let Ok(text) = text.cast_into_exact::<PyString>() else {
                return Ok(None);
            };
            let Ok(value) = text.extract::<String>() else {
                return Ok(None);
            };
            value
        } else {
            String::new()
        };
        let direction = item.get_item(pyo3::intern!(py, "writing_angle"))?;
        let direction = if let Some(value) = direction.filter(|value| !value.is_none()) {
            if !value.is_exact_instance_of::<PyFloat>() && !value.is_exact_instance_of::<PyInt>() {
                return Ok(None);
            }
            let Ok(value) = value.extract::<f64>() else {
                return Ok(None);
            };
            let degrees = value.to_degrees();
            if degrees.is_finite() {
                let relative = (degrees.rem_euclid(360.0) - angle as f64).rem_euclid(360.0);
                let cardinal = ((relative / 90.0).round_ties_even() as i32 * 90) % 360;
                (((relative - cardinal as f64 + 180.0).rem_euclid(360.0) - 180.0).abs() <= 0.1)
                    .then_some(cardinal)
            } else {
                None
            }
        } else {
            // 没有书写方向的历史输入保留原有整表坐标解释。
            Some(0)
        };
        let advance = if direction.is_some_and(|value| value != 0) {
            if let Some(raw) = item
                .get_item(pyo3::intern!(py, "loose_bbox"))?
                .filter(|value| !value.is_none())
            {
                let Some(value) = bbox(&raw, bbox_type)? else {
                    return Ok(None);
                };
                Some(value)
            } else {
                None
            }
        } else {
            None
        };
        let raw_origin = item.get_item(pyo3::intern!(py, "origin"))?;
        if let Some(raw) = raw_origin.as_ref() {
            if (raw.is_exact_instance_of::<PyTuple>() || raw.is_exact_instance_of::<PyList>())
                && raw.len()? == 2
            {
                for coordinate in raw.try_iter()? {
                    let coordinate = coordinate?;
                    if !coordinate.is_exact_instance_of::<PyFloat>()
                        && !coordinate.is_exact_instance_of::<PyInt>()
                    {
                        return Ok(None);
                    }
                }
            }
        }
        values.push(b);
        advances.push(advance);
        source.push((index, text, direction, origin(raw_origin, table, angle)));
    }
    let mut selected = py.detach(|| docvortex_core::tables::select_boxes(values, table, angle));
    let advances: HashMap<_, _> = if advances.iter().any(Option::is_some) {
        py.detach(|| docvortex_core::tables::select_boxes(advances, table, angle))
            .into_iter()
            .filter_map(|(index, _, local)| local.map(|value| (index, value)))
            .collect()
    } else {
        HashMap::new()
    };
    selected.sort_by_key(|&(i, _, _)| (source[i].0, i));
    let keys = [
        pyo3::intern!(py, "glyph_id"),
        pyo3::intern!(py, "source_index"),
        pyo3::intern!(py, "text"),
        pyo3::intern!(py, "bbox"),
        pyo3::intern!(py, "explicit_space_before"),
        pyo3::intern!(py, "explicit_break_before"),
        pyo3::intern!(py, "writing_angle"),
        pyo3::intern!(py, "advance_bbox"),
        pyo3::intern!(py, "origin"),
    ];
    let output = PyList::empty(py);
    let mut cache = HashMap::<String, (bool, String)>::new();
    let mut pending_space = false;
    let mut pending_break = false;
    for (i, _, local) in selected {
        let (index, text, direction, origin) = &source[i];
        if text.is_empty() {
            continue;
        }
        if text == "\r" || text == "\n" {
            pending_break = true;
            pending_space = false;
            continue;
        }
        let (space, normalized) = if let Some(value) = cache.get(text) {
            value.clone()
        } else {
            let text_py = PyString::new(py, text);
            let space = text_py
                .call_method0(pyo3::intern!(py, "isspace"))?
                .extract::<bool>()?;
            let normalized = if space {
                String::new()
            } else {
                normalizer.call1((text_py,))?.extract::<String>()?
            };
            cache.insert(text.clone(), (space, normalized.clone()));
            (space, normalized)
        };
        if space {
            if !pending_break {
                pending_space = true;
            }
            continue;
        }
        let normalized_py = PyString::new(py, &normalized);
        if normalized.is_empty()
            || normalized_py
                .call_method0(pyo3::intern!(py, "isspace"))?
                .extract::<bool>()?
        {
            continue;
        }
        let Some(local) = local else {
            continue;
        };
        // 汉字单横笔画的墨迹框可能不足半点；字符存在时不能丢失小节号等正文。
        let contains_cjk = normalized
            .chars()
            .any(|c| matches!(c, '\u{3400}'..='\u{9fff}'));
        if local[3] - local[1] < 0.5 && !contains_cjk {
            continue;
        }
        let record = allocator.call1((record_type,))?;
        let fields: [Bound<'py, PyAny>; 9] = [
            output.len().into_pyobject(py)?.into_any(),
            index.into_pyobject(py)?.into_any(),
            normalized_py.into_any(),
            super::conversion::box_tuple(local)
                .into_pyobject(py)?
                .into_any(),
            pending_space.into_pyobject(py)?.to_owned().into_any(),
            pending_break.into_pyobject(py)?.to_owned().into_any(),
            direction.into_pyobject(py)?.into_any(),
            advances
                .get(&i)
                .copied()
                .map(super::conversion::box_tuple)
                .into_pyobject(py)?
                .into_any(),
            origin.into_pyobject(py)?.into_any(),
        ];
        for (key, value) in keys.iter().zip(&fields) {
            super::text_projection::set_frozen_slot(py, &record, key, value)?;
        }
        output.append(record)?;
        pending_space = false;
        pending_break = false;
    }
    Ok(Some(output))
}
