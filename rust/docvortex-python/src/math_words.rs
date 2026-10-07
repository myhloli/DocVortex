//! 原始行字形的文字与字体证据批量读取，正则边界仍由当前 Python 实现裁决。
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyFrozenSet, PyInt, PyList, PySet, PyString};
use std::collections::HashMap;

/// 仅转换普通有限字号与整数标志；其他可转换对象完整交回原 Python 路径。
fn plain_number(value: Option<Bound<'_, PyAny>>, flags: bool) -> Option<f64> {
    let Some(value) = value else { return Some(0.0) };
    if value.is_none() {
        return Some(0.0);
    }
    if flags {
        if value.is_exact_instance_of::<PyInt>() || value.is_exact_instance_of::<PyBool>() {
            return value
                .extract::<i64>()
                .ok()
                .filter(|v| v.unsigned_abs() <= 1 << 50)
                .map(|v| v as f64);
        }
        return None;
    }
    if value.is_exact_instance_of::<PyFloat>()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyBool>()
    {
        let number = value.extract::<f64>().ok()?;
        return (number.is_finite() && number.abs() <= 2f64.powi(50)).then_some(number);
    }
    None
}

/// 保留完整可打印文字、Python 正则偏移、原字形切片及最小字号首项语义；不缓存认领结果。
#[pyfunction]
pub(super) fn math_words_owned<'py>(
    py: Python<'py>,
    line: &Bound<'py, PyAny>,
    line_type: &Bound<'py, PyAny>,
    finder: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyFrozenSet>>> {
    if !line.get_type().is(line_type) {
        return Ok(None);
    }
    let raw_chars = line.getattr(pyo3::intern!(py, "chars"))?;
    let raw_words = line.getattr(pyo3::intern!(py, "native_math_words"))?;
    let (Ok(chars), Ok(words)) = (
        raw_chars.cast_exact::<PyList>(),
        raw_words.cast_exact::<PyFrozenSet>(),
    ) else {
        return Ok(None);
    };
    if words
        .iter()
        .any(|word| !word.is_exact_instance_of::<PyString>())
    {
        return Ok(None);
    }
    let mut printable = HashMap::new();
    let mut members = Vec::new();
    let mut text = String::new();
    for item in chars.iter() {
        let Ok(item) = item.cast_exact::<PyDict>() else {
            return Ok(None);
        };
        if !super::conversion::plain_string_keys(item) {
            return Ok(None);
        }
        let value = item.get_item(pyo3::intern!(py, "char"))?;
        let value = match value {
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
        let Ok(raw) = value.to_str() else {
            return Ok(None);
        };
        let keep = if let Some(keep) = printable.get(raw) {
            *keep
        } else {
            let keep = value
                .call_method0(pyo3::intern!(py, "isprintable"))?
                .extract::<bool>()?;
            printable.insert(raw.to_owned(), keep);
            keep
        };
        if keep {
            text.push_str(raw);
            members.push(item.clone());
        }
    }
    let output = PySet::new(py, words.iter())?;
    let matches = finder.call1((r"\b[a-z]{3,4}\b", text))?;
    for matched in matches.try_iter()? {
        let matched = matched?;
        let start = matched
            .call_method0(pyo3::intern!(py, "start"))?
            .extract::<usize>()?;
        let end = matched
            .call_method0(pyo3::intern!(py, "end"))?
            .extract::<usize>()?;
        let mut sizes = Vec::new();
        let mut italic = true;
        for member in members.iter().skip(start).take(end.saturating_sub(start)) {
            let raw_font = member.get_item(pyo3::intern!(py, "font"))?;
            let (size, flags) = match raw_font {
                None => (0.0, 0.0),
                Some(font) if font.is_none() => (0.0, 0.0),
                Some(font) => {
                    let Ok(font) = font.cast_exact::<PyDict>() else {
                        return Ok(None);
                    };
                    if !super::conversion::plain_string_keys(font) {
                        return Ok(None);
                    }
                    let (Some(size), Some(flags)) = (
                        plain_number(font.get_item(pyo3::intern!(py, "size"))?, false),
                        plain_number(font.get_item(pyo3::intern!(py, "flags"))?, true),
                    ) else {
                        return Ok(None);
                    };
                    (size, flags)
                }
            };
            sizes.push(size);
            italic &= (flags as i64 & 64) != 0;
        }
        if let Some(first) = sizes.first().copied() {
            let (mut low, mut high) = (first, first);
            for size in sizes.iter().copied().skip(1) {
                if size < low {
                    low = size;
                }
                if size > high {
                    high = size;
                }
            }
            if italic || low > 0.0 && low <= 0.85 * high {
                output.add(matched.call_method0(pyo3::intern!(py, "group"))?)?;
            }
        }
    }
    Ok(Some(PyFrozenSet::new(py, output.iter())?))
}
