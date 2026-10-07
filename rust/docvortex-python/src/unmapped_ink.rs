//! 当前字符的可打印状态与公式墨迹几何批量筛选；不缓存公式认领结果。
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use std::collections::HashMap;

/// 只接受普通可转换数值，特殊对象和溢出整数在原 Python 路径保留异常语义。
fn number(value: &Bound<'_, PyAny>) -> Option<f64> {
    if value.is_exact_instance_of::<PyFloat>()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyBool>()
    {
        value.extract().ok()
    } else {
        None
    }
}

/// 普通四元框按 Python 稳定排序规范化，退化及非有限框返回已知空结果。
fn bbox(value: Option<Bound<'_, PyAny>>) -> Option<Option<[f64; 4]>> {
    let Some(value) = value else {
        return Some(None);
    };
    if value.is_none() {
        return Some(None);
    }
    let values: Vec<Bound<'_, PyAny>> = if let Ok(values) = value.cast_exact::<PyTuple>() {
        values.iter().collect()
    } else if let Ok(values) = value.cast_exact::<PyList>() {
        values.iter().collect()
    } else {
        return None;
    };
    if values.len() != 4 {
        // 原路径在解包前转换全部元素，异常长度也可能触发转换错误，必须完整回退。
        return None;
    }
    let mut result = [0.0; 4];
    for (target, value) in result.iter_mut().zip(values.iter()) {
        *target = number(value)?;
    }
    if result[2] < result[0] {
        result.swap(0, 2);
    }
    if result[3] < result[1] {
        result.swap(1, 3);
    }
    Some(
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
        let Some(box_value) = bbox(char.get_item(pyo3::intern!(py, "tight_bbox"))?) else {
            return Ok(None);
        };
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
                    Some(value) if value.is_none() => 0.0,
                    Some(value) => {
                        let Some(size) = number(&value) else {
                            return Ok(None);
                        };
                        size
                    }
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
            if let Some(value) = number(&value) {
                if value == 3.0 || value == 7.0 {
                    continue;
                }
            } else if !value.is_none() && !value.is_exact_instance_of::<PyString>() {
                return Ok(None);
            }
        }
        if b[2] - b[0] > 0.05 * size && b[3] - b[1] > 1.5 * size {
            output.push((b[0], b[1], b[2], b[3]));
        }
    }
    Ok(Some(output))
}
