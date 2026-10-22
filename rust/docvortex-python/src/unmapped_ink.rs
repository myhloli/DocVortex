//! 当前字符的可打印状态与公式墨迹几何批量筛选；不缓存公式认领结果。
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString};
use std::collections::HashMap;

/// 由解释器进行原有 float 转换，保留字符串、巨大整数及转换错误的语义。
fn number(value: &Bound<'_, PyAny>) -> PyResult<f64> {
    value.py().get_type::<PyFloat>().call1((value,))?.extract()
}

/// 完整转换坐标后再检查长度与有限性，转换溢出传播而无效形状返回空几何。
fn bbox(value: Option<Bound<'_, PyAny>>) -> PyResult<Option<[f64; 4]>> {
    let Some(value) = value else {
        return Ok(None);
    };
    let py = value.py();
    let iterator = match value.try_iter() {
        Ok(iterator) => iterator,
        Err(error) if error.is_instance_of::<pyo3::exceptions::PyTypeError>(py) => return Ok(None),
        Err(error) => return Err(error),
    };
    let mut values = Vec::new();
    for item in iterator {
        match number(&item?) {
            Ok(number) => values.push(number),
            Err(error)
                if error.is_instance_of::<pyo3::exceptions::PyTypeError>(py)
                    || error.is_instance_of::<pyo3::exceptions::PyValueError>(py) =>
            {
                return Ok(None)
            }
            Err(error) => return Err(error),
        }
    }
    let Ok(mut result) = <[f64; 4]>::try_from(values) else {
        return Ok(None);
    };
    if result[2] < result[0] {
        result.swap(0, 2);
    }
    if result[3] < result[1] {
        result.swap(1, 3);
    }
    Ok(
        (result.iter().all(|v| v.is_finite()) && result[2] > result[0] && result[3] > result[1])
            .then_some(result),
    )
}

/// 每页读取当前全部字形；重复文字复用不可变类别，字号、可见性与几何每次重新读取。
#[pyfunction]
pub(super) fn unmapped_formula_ink<'py>(
    py: Python<'py>,
    chars: &Bound<'py, PyAny>,
    glyph_flags: &Bound<'py, PyAny>,
) -> PyResult<Option<Vec<(f64, f64, f64, f64)>>> {
    let Ok(chars) = chars.cast_exact::<PyList>() else {
        return Ok(None);
    };
    let mut cache = HashMap::new();
    let mut output = Vec::new();
    for char in chars.iter() {
        let Ok(char) = char.cast_exact::<PyDict>() else {
            return Ok(None);
        };
        if !super::conversion::plain_string_keys(char) {
            return Ok(None);
        }
        let text = match char.get_item(pyo3::intern!(py, "char"))? {
            None => PyString::new(py, ""),
            Some(value) if value.is_none() => PyString::new(py, "None"),
            Some(value) => {
                let Ok(value) = value.cast_into::<PyString>() else {
                    return Ok(None);
                };
                if !value.is_exact_instance_of::<PyString>() {
                    return Ok(None);
                }
                value
            }
        };
        let Ok(raw) = text.to_str() else {
            return Ok(None);
        };
        let visible = if raw.is_ascii() {
            raw.bytes().all(|v| (32..=126).contains(&v))
                && (raw.is_empty() || raw.bytes().any(|v| v != 32))
        } else if let Some(visible) = cache.get(raw) {
            *visible
        } else {
            let visible = glyph_flags.call1((&text,))?.extract::<u8>()? & 1 != 0;
            cache.insert(raw.to_owned(), visible);
            visible
        };
        if visible {
            continue;
        }
        let box_value = bbox(char.get_item(pyo3::intern!(py, "tight_bbox"))?)?;
        let size = match char.get_item(pyo3::intern!(py, "font"))? {
            None => 0.0,
            Some(value) if value.is_none() => 0.0,
            Some(value) => {
                let Ok(font) = value.cast_exact::<PyDict>() else {
                    return Ok(None);
                };
                if !super::conversion::plain_string_keys(font) {
                    return Ok(None);
                }
                match font.get_item(pyo3::intern!(py, "size"))? {
                    None => 0.0,
                    Some(value) if value.is_truthy()? => number(&value)?,
                    Some(_) => 0.0,
                }
            }
        };
        let Some(b) = box_value else {
            continue;
        };
        if size.is_nan() || size <= 0.0 {
            continue;
        }
        if char
            .get_item(pyo3::intern!(py, "text_object_id"))?
            .is_none_or(|v| v.is_none())
        {
            continue;
        }
        let mode = char.get_item(pyo3::intern!(py, "text_render_mode"))?;
        if let Some(value) = mode {
            if value.is_instance_of::<PyInt>()
                || value.is_instance_of::<PyFloat>()
                || value.is_instance_of::<PyBool>()
            {
                let mode = number(&value)?;
                if mode == 3.0 || mode == 7.0 {
                    continue;
                }
            } else if !value.is_none() && !value.is_instance_of::<PyString>() {
                return Ok(None);
            }
        }
        if b[2] - b[0] > 0.05 * size && b[3] - b[1] > 1.5 * size {
            output.push((b[0], b[1], b[2], b[3]));
        }
    }
    Ok(Some(output))
}
