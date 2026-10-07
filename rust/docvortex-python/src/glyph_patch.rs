//! 字形粗体的字体匹配一次扫描；当前字符字典和字体字段变化后重新读取。
// 高频字段名按解释器复用不可变字符串，仍由原 Python 属性和字典操作读取当前状态。
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PySet, PyString};
use std::collections::HashMap;

/// 仅内置字体字段可复用标准转换，字典子类及自定义转换对象完整回退。
fn plain_field(value: &Bound<'_, PyAny>) -> bool {
    value.is_none()
        || value.is_exact_instance_of::<PyString>()
        || value.is_exact_instance_of::<PyFloat>()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyBool>()
}

/// 先验证全部字符，再逐字体调用原转换，输出原行内全部粗体字符位置，保留成员次序。
#[pyfunction]
pub(super) fn glyph_bold_indices_owned(
    lines: &Bound<'_, PyAny>,
    bold: &Bound<'_, PyAny>,
    metadata: &Bound<'_, PyAny>,
    line_type: &Bound<'_, PyAny>,
) -> PyResult<Option<Vec<(usize, Vec<usize>)>>> {
    if !lines.is_exact_instance_of::<PyList>() || !bold.is_exact_instance_of::<PySet>() {
        return Ok(None);
    }
    let bold = bold.cast::<PySet>()?;
    let mut rows = Vec::new();
    for line in lines.try_iter()? {
        let line = line?;
        if !line.get_type().is(line_type) {
            return Ok(None);
        }
        let chars = line.getattr(pyo3::intern!(line.py(), "chars"))?;
        if !chars.is_exact_instance_of::<PyList>() {
            return Ok(None);
        }
        let mut row = Vec::new();
        for ch in chars.try_iter()? {
            let ch = ch?;
            if !ch.is_exact_instance_of::<PyDict>() {
                return Ok(None);
            }
            let font = ch.cast::<PyDict>()?.get_item("font")?;
            let key = if let Some(font) = font {
                if font.is_instance_of::<PyDict>() {
                    if !font.is_exact_instance_of::<PyDict>() {
                        return Ok(None);
                    }
                    for name in ["name", "flags", "weight"] {
                        if font
                            .cast::<PyDict>()?
                            .get_item(name)?
                            .is_some_and(|value| !plain_field(&value))
                        {
                            return Ok(None);
                        }
                    }
                    font.as_ptr() as usize
                } else {
                    0
                }
            } else {
                0
            };
            row.push((ch, key));
        }
        rows.push(row);
    }
    let mut converted = HashMap::new();
    let mut output = Vec::new();
    for (row_index, row) in rows.iter().enumerate() {
        let mut indices = Vec::new();
        for (char_index, (ch, key)) in row.iter().enumerate() {
            let matched = if let Some(&matched) = converted.get(key) {
                matched
            } else {
                let matched = bold.contains(metadata.call1((ch,))?)?;
                converted.insert(*key, matched);
                matched
            };
            if matched {
                indices.push(char_index);
            }
        }
        if !indices.is_empty() {
            output.push((row_index, indices));
        }
    }
    Ok(Some(output))
}
