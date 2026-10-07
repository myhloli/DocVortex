//! 分式几何的当前状态批处理；字号、字符和框不会在页面阶段之间缓存。
// 高频字段名按解释器复用不可变字符串，仍由原 Python 属性和字典操作读取当前状态。
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyInt, PyList, PySet, PyString, PyTuple};
use std::collections::HashMap;

/// 只接受原生有限浮点框，避免将任意精度整数运算或自定义取值隐式改为双精度。
fn float_box(value: &Bound<'_, PyAny>) -> Option<[f64; 4]> {
    if (!value.is_exact_instance_of::<PyList>() && !value.is_exact_instance_of::<PyTuple>())
        || value.len().ok()? != 4
    {
        return None;
    }
    let mut out = [0.0_f64; 4];
    for (i, coordinate) in out.iter_mut().enumerate() {
        let obj = value.get_item(i).ok()?;
        if !obj.is_exact_instance_of::<PyFloat>() {
            return None;
        }
        *coordinate = obj.extract().ok()?;
        if !coordinate.is_finite() || coordinate.abs() > 2f64.powi(50) {
            return None;
        }
    }
    Some(out)
}

/// 按原坐标减法次序旋转普通字符框；未知角度保持原框，和 Python 分支一致。
fn rotate(b: [f64; 4], page: [f64; 2], angle: i32) -> [f64; 4] {
    match angle {
        270 => [page[1] - b[3], b[0], page[1] - b[1], b[2]],
        90 => [b[1], page[0] - b[2], b[3], page[0] - b[0]],
        180 => [
            page[0] - b[2],
            page[1] - b[3],
            page[0] - b[0],
            page[1] - b[1],
        ],
        _ => b,
    }
}

/// 完整验证后一次读取所有字符；Unicode 判定调用当前 Python 内置方法，每个不同文本只算一次。
#[pyfunction]
#[pyo3(signature = (chars, tight, page, angle, rules, rule_type=None))]
pub(super) fn fraction_members_owned<'py>(
    py: Python<'py>,
    chars: &Bound<'py, PyAny>,
    tight: &Bound<'py, PyAny>,
    page: &Bound<'py, PyAny>,
    angle: &Bound<'py, PyAny>,
    rules: &Bound<'py, PyAny>,
    rule_type: Option<&Bound<'py, PyAny>>,
) -> PyResult<Option<Bound<'py, PySet>>> {
    if !chars.is_exact_instance_of::<PyList>()
        || !tight.is_exact_instance_of::<PyDict>()
        || !angle.is_exact_instance_of::<PyInt>()
        || (!rules.is_exact_instance_of::<PyList>() && !rules.is_exact_instance_of::<PyTuple>())
        || (!page.is_exact_instance_of::<PyList>() && !page.is_exact_instance_of::<PyTuple>())
        || page.len()? != 2
    {
        return Ok(None);
    }
    let Ok(angle) = angle.extract::<i32>() else {
        return Ok(None);
    };
    let mut page_size = [0.0_f64; 2];
    for (i, value) in page_size.iter_mut().enumerate() {
        let obj = page.get_item(i)?;
        if !obj.is_exact_instance_of::<PyFloat>() && !obj.is_exact_instance_of::<PyInt>() {
            return Ok(None);
        }
        let Ok(number) = obj.extract::<f64>() else {
            return Ok(None);
        };
        if !number.is_finite() || number.abs() > 2f64.powi(24) {
            return Ok(None);
        }
        *value = number;
    }
    let mut local_rules = Vec::new();
    for rule in rules.try_iter()? {
        let rule = rule?;
        let rule = if let Some(rule_type) = rule_type {
            if !rule.get_type().is(rule_type) {
                return Ok(None);
            }
            rule.getattr(pyo3::intern!(py, "bbox"))?
        } else {
            rule
        };
        if rule.is_none() {
            continue;
        }
        let Some(mut b) = float_box(&rule) else {
            return Ok(None);
        };
        if rule_type.is_some() {
            // 原 _coerce_bbox 仅接收非退化有限框；原始绘图框先验证再沿相同方向旋转。
            if b[2] <= b[0] || b[3] <= b[1] {
                continue;
            }
            b = rotate(b, page_size, angle);
        }
        local_rules.push(b);
    }
    let mut packed = Vec::new();
    for char in chars.try_iter()? {
        let char = char?;
        let Ok(char) = char.cast_exact::<PyDict>() else {
            return Ok(None);
        };
        let text = char
            .get_item(pyo3::intern!(char.py(), "char"))?
            .unwrap_or_else(|| PyString::new(py, "").into_any());
        let Ok(text) = text.cast_into_exact::<PyString>() else {
            return Ok(None);
        };
        let Ok(key) = text.extract::<String>() else {
            return Ok(None);
        };
        let Some(index) = char.get_item(pyo3::intern!(char.py(), "char_idx"))? else {
            continue;
        };
        if !index.is_exact_instance_of::<PyInt>() {
            return Ok(None);
        }
        let Some(b) = tight.cast::<PyDict>()?.get_item(&index)? else {
            continue;
        };
        if b.is_none() {
            continue;
        }
        let Some(b) = float_box(&b) else {
            return Ok(None);
        };
        packed.push((index, text, key, rotate(b, page_size, angle)));
    }
    let mut features = HashMap::new();
    let mut records = Vec::new();
    let mut indices = Vec::new();
    let mut heights = Vec::new();
    for (index, text, key, b) in packed {
        let (visible, alnum) = if let Some(&feature) = features.get(&key) {
            feature
        } else {
            let visible = text.call_method0("isprintable")?.extract::<bool>()?
                && !text.call_method0("isspace")?.extract::<bool>()?;
            let alnum = visible && text.call_method0("isalnum")?.extract::<bool>()?;
            features.insert(key, (visible, alnum));
            (visible, alnum)
        };
        if !visible {
            continue;
        }
        records.push((b, alnum));
        indices.push(index);
        if b[3] - b[1] > 0.0 {
            heights.push(b[3] - b[1]);
        }
    }
    if !records.is_empty() && heights.is_empty() {
        return Ok(None);
    }
    let scale = if records.is_empty() {
        8.0
    } else {
        docvortex_core::median(heights)
    };
    let members =
        py.detach(|| docvortex_core::rule_text::fraction_members(&records, &local_rules, scale));
    let result = PySet::empty(py)?;
    for i in members {
        result.add(&indices[i])?;
    }
    Ok(Some(result))
}
