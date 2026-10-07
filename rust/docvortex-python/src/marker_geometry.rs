//! 表注来源校验及字形准备批量读取当前行，不跨页面缓存文字、几何或认领结果。
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use std::collections::HashMap;

/// 严格复用原上下文准入范围，任意精度整数只有原先允许的区间才可进入。
fn marker_box(
    value: &Bound<'_, PyAny>,
    bbox_type: &Bound<'_, PyAny>,
    floats_only: bool,
) -> Option<[f64; 4]> {
    let raw = if value.get_type().is(bbox_type) {
        value.getattr(pyo3::intern!(value.py(), "bbox")).ok()?
    } else {
        value.clone()
    };
    if (!raw.is_exact_instance_of::<PyList>() && !raw.is_exact_instance_of::<PyTuple>())
        || raw.len().ok()? != 4
    {
        return None;
    }
    let mut out = [0.0; 4];
    for (i, number) in out.iter_mut().enumerate() {
        let value = raw.get_item(i).ok()?;
        if value.is_exact_instance_of::<PyFloat>() {
            *number = value.extract().ok()?;
        } else if !floats_only && value.is_exact_instance_of::<PyInt>() {
            let integer = value.extract::<i64>().ok()?;
            if integer.unsigned_abs() > 1 << 53 {
                return None;
            }
            *number = integer as f64;
        } else {
            return None;
        }
        if !number.is_finite() {
            return None;
        }
    }
    Some(out)
}

/// 全页一次验证字符形状与有限坐标，按原来源整数及首见顺序返回完整行对象。
#[pyfunction]
pub(super) fn marker_sources_owned<'py>(
    py: Python<'py>,
    lines: &Bound<'py, PyAny>,
    line_type: &Bound<'py, PyAny>,
    bbox_type: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyDict>>> {
    let Ok(lines) = lines.cast_exact::<PyList>() else {
        return Ok(None);
    };
    let output = PyDict::new(py);
    for line in lines.iter() {
        if !line.get_type().is(line_type)
            || !line
                .getattr(pyo3::intern!(py, "text"))?
                .is_exact_instance_of::<PyString>()
        {
            return Ok(None);
        }
        let source = line.getattr(pyo3::intern!(py, "source_index"))?;
        if !source.is_exact_instance_of::<PyInt>() {
            return Ok(None);
        }
        let raw_chars = line.getattr(pyo3::intern!(py, "chars"))?;
        let Ok(chars) = raw_chars.cast_exact::<PyList>() else {
            return Ok(None);
        };
        for char in chars.iter() {
            let Ok(char) = char.cast_exact::<PyDict>() else {
                return Ok(None);
            };
            if !super::conversion::plain_string_keys(char) {
                return Ok(None);
            }
            if char
                .get_item(pyo3::intern!(py, "char"))?
                .is_some_and(|text| !text.is_none() && !text.is_exact_instance_of::<PyString>())
            {
                return Ok(None);
            }
            if let Some(bbox) = char.get_item(pyo3::intern!(py, "bbox"))? {
                if !bbox.is_none() && marker_box(&bbox, bbox_type, false).is_none() {
                    return Ok(None);
                }
            }
        }
        let list = match output.get_item(&source)? {
            Some(list) => list.cast_into::<PyList>()?,
            None => {
                let list = PyList::empty(py);
                output.set_item(&source, &list)?;
                list
            }
        };
        list.append(line)?;
    }
    Ok(Some(output))
}

/// 全量验证之后再使用原 Unicode 规范化与紧凑 token，重复字符只在本次输入状态内处理一次。
#[pyfunction]
pub(super) fn marker_glyphs_owned<'py>(
    py: Python<'py>,
    line: &Bound<'py, PyAny>,
    page: &Bound<'py, PyAny>,
    angle: &Bound<'py, PyAny>,
    line_type: &Bound<'py, PyAny>,
    bbox_type: &Bound<'py, PyAny>,
    normalizer: &Bound<'py, PyAny>,
    compact: &Bound<'py, PyAny>,
) -> PyResult<Option<(Bound<'py, PyList>, Bound<'py, PyAny>)>> {
    if !line.get_type().is(line_type)
        || !angle.is_exact_instance_of::<PyInt>()
        || (!page.is_exact_instance_of::<PyTuple>() && !page.is_exact_instance_of::<PyList>())
        || page.len()? != 2
    {
        return Ok(None);
    }
    let Ok(angle) = angle.extract::<i32>() else {
        return Ok(None);
    };
    let mut size = [0.0; 2];
    for (i, number) in size.iter_mut().enumerate() {
        let value = page.get_item(i)?;
        if !value.is_exact_instance_of::<PyFloat>() {
            return Ok(None);
        }
        *number = value.extract::<f64>()?;
        if !number.is_finite() {
            return Ok(None);
        }
    }
    let text = line.getattr(pyo3::intern!(py, "text"))?;
    if !text.is_exact_instance_of::<PyString>() {
        return Ok(None);
    }
    let raw_chars = line.getattr(pyo3::intern!(py, "chars"))?;
    let Ok(chars) = raw_chars.cast_exact::<PyList>() else {
        return Ok(None);
    };
    let mut inputs = Vec::new();
    for char in chars.iter() {
        let Ok(char) = char.cast_exact::<PyDict>() else {
            return Ok(None);
        };
        if !super::conversion::plain_string_keys(char) {
            return Ok(None);
        }
        let value = match char.get_item(pyo3::intern!(py, "char"))? {
            None => PyString::new(py, ""),
            Some(value) if value.is_none() => PyString::new(py, ""),
            Some(value) => {
                if !value.is_exact_instance_of::<PyString>() {
                    return Ok(None);
                }
                value.cast_into::<PyString>()?
            }
        };
        if value.to_str().is_err() {
            return Ok(None);
        }
        let bbox = match char.get_item(pyo3::intern!(py, "bbox"))? {
            None => None,
            Some(value) if value.is_none() => None,
            Some(value) => {
                let Some(bbox) = marker_box(&value, bbox_type, true) else {
                    return Ok(None);
                };
                Some(bbox)
            }
        };
        inputs.push((value, bbox));
    }
    let output = PyList::empty(py);
    let mut features: HashMap<String, Option<Bound<'py, PyAny>>> = HashMap::new();
    for (value, bbox) in inputs {
        let raw = value.to_str()?;
        let normalized = if let Some(feature) = features.get(raw) {
            feature.clone()
        } else {
            let feature = if value
                .call_method0(pyo3::intern!(py, "isprintable"))?
                .extract::<bool>()?
                && !value
                    .call_method0(pyo3::intern!(py, "isspace"))?
                    .extract::<bool>()?
            {
                Some(
                    normalizer
                        .call1(("NFKC", &value))?
                        .call_method0(pyo3::intern!(py, "casefold"))?,
                )
            } else {
                None
            };
            features.insert(raw.to_owned(), feature.clone());
            feature
        };
        let (Some(normalized), Some(mut bbox)) = (normalized, bbox) else {
            continue;
        };
        if bbox[2] < bbox[0] {
            bbox.swap(0, 2);
        }
        if bbox[3] < bbox[1] {
            bbox.swap(1, 3);
        }
        if bbox[2] <= bbox[0] || bbox[3] <= bbox[1] {
            continue;
        }
        let bbox = docvortex_core::geometry::rotate(bbox, size, angle);
        output.append((normalized, (bbox[0], bbox[1], bbox[2], bbox[3])))?;
    }
    Ok(Some((output, compact.call1((text,))?)))
}
