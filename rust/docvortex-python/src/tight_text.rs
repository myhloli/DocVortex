//! 批量预计算表格字符补空格的必要条件；最终角色和几何仍由原 Python 判定。
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyInt, PySet, PyString, PyTuple};
use std::collections::HashMap;

/// 中日韩范围逐项沿用 Python 排除表，不以 Rust 的 Unicode 分类替代现有语义。
fn excluded_cjk(c: char) -> bool {
    let c = c as u32;
    [
        (0x1100, 0x11ff),
        (0x2e80, 0xa4cf),
        (0xa960, 0xa97f),
        (0xac00, 0xd7ff),
        (0xf900, 0xfaff),
        (0xff66, 0xff9f),
        (0x1aff0, 0x1b16f),
        (0x20000, 0x323af),
    ]
    .iter()
    .any(|(a, b)| *a <= c && c <= *b)
}

/// 当前源索引与文字必须属于普通字典；未知转换和代理对象完整回退。
fn features(
    py: Python<'_>,
    chars: &Bound<'_, PyDict>,
    index: i64,
    ordinary: &Bound<'_, PyAny>,
    cache: &mut HashMap<i64, (bool, bool)>,
) -> PyResult<Option<(bool, bool)>> {
    if let Some(flags) = cache.get(&index) {
        return Ok(Some(*flags));
    }
    let Some(char) = chars.get_item(index)? else {
        return Ok(Some((false, false)));
    };
    let Ok(char) = char.cast_exact::<PyDict>() else {
        return Ok(None);
    };
    if !super::conversion::plain_string_keys(char) {
        return Ok(None);
    }
    let Some(source) = char.get_item(pyo3::intern!(py, "char_idx"))? else {
        return Ok(None);
    };
    if !source.is_exact_instance_of::<PyInt>() || source.extract::<i64>().ok() != Some(index) {
        return Ok(None);
    }
    let text = match char.get_item(pyo3::intern!(py, "char"))? {
        None => PyString::new(py, ""),
        Some(text) if text.is_none() => PyString::new(py, "None"),
        Some(text) => {
            let Ok(text) = text.cast_into::<PyString>() else {
                return Ok(None);
            };
            if !text.is_exact_instance_of::<PyString>() {
                return Ok(None);
            }
            text
        }
    };
    let Ok(raw) = text.to_str() else {
        return Ok(None);
    };
    let mut codes = raw.chars();
    let first = codes.next();
    let flags = match first.filter(|_| codes.next().is_none()) {
        None => (false, false),
        Some(c) if excluded_cjk(c) => (false, false),
        Some(c) if c.is_ascii() => (c.is_ascii_alphanumeric(), c.is_ascii_digit()),
        Some(_) => (
            ordinary.call1((&text,))?.extract::<bool>()?,
            text.call_method0(pyo3::intern!(py, "isdecimal"))?
                .extract::<bool>()?,
        ),
    };
    cache.insert(index, flags);
    Ok(Some(flags))
}

/// 完整覆盖本表字形，返回仅满足文字和连续源索引必要条件的右索引，其他字段不做缓存。
#[pyfunction]
pub(super) fn tight_space_candidates<'py>(
    py: Python<'py>,
    chars_by_source: &Bound<'py, PyAny>,
    glyphs: &Bound<'py, PyAny>,
    glyph_type: &Bound<'py, PyAny>,
    ordinary: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PySet>>> {
    let (Ok(chars), Ok(glyphs)) = (
        chars_by_source.cast_exact::<PyDict>(),
        glyphs.cast_exact::<PyTuple>(),
    ) else {
        return Ok(None);
    };
    if !super::conversion::plain_integer_keys(chars) {
        return Ok(None);
    }
    let mut cache = HashMap::new();
    let output = PySet::empty(py)?;
    for glyph in glyphs.iter() {
        if !glyph.get_type().is(glyph_type) {
            return Ok(None);
        }
        let index = glyph.getattr(pyo3::intern!(py, "source_index"))?;
        if !index.is_exact_instance_of::<PyInt>() {
            return Ok(None);
        }
        let Ok(index) = index.extract::<i64>() else {
            return Ok(None);
        };
        let Some((right, right_decimal)) = features(py, chars, index, ordinary, &mut cache)? else {
            return Ok(None);
        };
        if !right {
            continue;
        }
        let Some(left_index) = index.checked_sub(1) else {
            return Ok(None);
        };
        let Some((left, left_decimal)) = features(py, chars, left_index, ordinary, &mut cache)?
        else {
            return Ok(None);
        };
        if left && !(left_decimal && right_decimal) {
            output.add(index)?;
        }
    }
    Ok(Some(output))
}
