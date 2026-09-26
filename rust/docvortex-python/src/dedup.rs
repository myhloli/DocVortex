//! 重复绘制、隐藏文字候选与映射分组的批量绑定。

use docvortex_core::dedup;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple, PyType};
use std::collections::{HashMap, HashSet};

/// 普通整数使用有界读取，超大或自定义整数由 Python 参考实现处理。
fn plain_integer(value: &Bound<'_, PyAny>) -> Option<i64> {
    value
        .is_exact_instance_of::<PyInt>()
        .then(|| value.extract::<i64>().ok())
        .flatten()
}

/// 直接借用现有字符列表，一次准备映射保护区间与 Glyph 行框，不重新打包旧分组内核。
#[pyfunction]
pub(super) fn mapping_glyph_rows<'py>(
    py: Python<'py>,
    chars: &Bound<'py, PyList>,
    bbox_type: &Bound<'py, PyType>,
) -> PyResult<Option<Vec<(usize, usize, Option<Bound<'py, PyTuple>>, f64)>>> {
    let mut fonts = HashSet::new();
    let mut text_flags = HashMap::new();
    let mut output: Vec<(usize, usize, Option<Bound<'py, PyTuple>>, f64)> = Vec::new();
    let mut previous: Option<(
        Bound<'py, PyAny>,
        i64,
        Bound<'py, PyDict>,
        f64,
        Option<[f64; 2]>,
        [f64; 4],
        bool,
    )> = None;
    let mut head_text: Option<Bound<'py, PyAny>> = None;
    for (position, item) in chars.iter().enumerate() {
        if !item.is_exact_instance_of::<PyDict>() {
            return Ok(None);
        }
        let char = item.cast::<PyDict>()?;
        if char
            .iter()
            .any(|(key, _value)| !key.is_exact_instance_of::<PyString>())
        {
            return Ok(None);
        }
        let (Some(text), Some(index), Some(font), Some(raw), Some(rotation)) = (
            char.get_item(pyo3::intern!(py, "char"))?,
            char.get_item(pyo3::intern!(py, "char_idx"))?,
            char.get_item(pyo3::intern!(py, "font"))?,
            char.get_item(pyo3::intern!(py, "bbox"))?,
            char.get_item(pyo3::intern!(py, "rotation"))?,
        ) else {
            return Ok(None);
        };
        if !text.is_exact_instance_of::<PyString>()
            || !font.is_exact_instance_of::<PyDict>()
            || !rotation.is_exact_instance_of::<PyFloat>()
        {
            return Ok(None);
        }
        let Some(index) = plain_integer(&index) else {
            return Ok(None);
        };
        let font = font.cast_into::<PyDict>()?;
        let font_id = font.as_ptr() as usize;
        if !fonts.contains(&font_id) {
            for (key, value) in font.iter() {
                if !key.is_exact_instance_of::<PyString>()
                    || !(value.is_none()
                        || value.is_exact_instance_of::<PyString>()
                        || value.is_exact_instance_of::<PyInt>()
                        || value.is_exact_instance_of::<PyFloat>()
                        || value.is_exact_instance_of::<PyBool>())
                {
                    return Ok(None);
                }
            }
            if fonts.len() >= 4096 {
                fonts.clear();
            }
            fonts.insert(font_id);
        }
        let raw = if raw.get_type().is(bbox_type) {
            raw.getattr(pyo3::intern!(py, "bbox"))?
        } else {
            raw
        };
        let Some(Some(box4)) = super::geometry::plain_coordinates::<4>(&raw) else {
            return Ok(None);
        };
        let origin = char
            .get_item(pyo3::intern!(py, "origin"))?
            .unwrap_or_else(|| py.None().into_bound(py));
        let Some(origin) = super::geometry::plain_coordinates::<2>(&origin) else {
            return Ok(None);
        };
        let object = char
            .get_item(pyo3::intern!(py, "text_object_id"))?
            .unwrap_or_else(|| py.None().into_bound(py));
        if !object.is_none() && !object.is_exact_instance_of::<PyInt>() {
            return Ok(None);
        }
        let rotation = rotation.extract::<f64>()?;
        let angle = if let Some(value) = char.get_item(pyo3::intern!(py, "writing_angle"))? {
            if !value.is_exact_instance_of::<PyFloat>() {
                return Ok(None);
            }
            value.extract::<f64>()?
        } else {
            -rotation
        };
        if !rotation.is_finite() || !angle.is_finite() {
            return Ok(None);
        }
        let last_source =
            if let Some(source) = char.get_item(pyo3::intern!(py, "source_indices"))? {
                if !source.is_exact_instance_of::<PyTuple>() || source.len()? == 0 {
                    return Ok(None);
                }
                let mut maximum = i64::MIN;
                for value in source.cast::<PyTuple>()?.iter() {
                    let Some(value) = plain_integer(&value) else {
                        return Ok(None);
                    };
                    maximum = maximum.max(value);
                }
                maximum
            } else {
                index
            };
        let Ok(text_key) = text.cast::<PyString>()?.to_str() else {
            return Ok(None);
        };
        let has_text = if let Some(value) = text_flags.get(text_key) {
            *value
        } else {
            let value = text.call_method0(pyo3::intern!(py, "strip"))?.is_truthy()?;
            if text_key.len() <= 32 {
                if text_flags.len() >= 4096 {
                    text_flags.clear();
                }
                text_flags.insert(text_key.to_owned(), value);
            }
            value
        };
        let same = if let Some((
            obj,
            maximum,
            previous_font,
            previous_rotation,
            previous_origin,
            previous_box,
            previous_text,
        )) = &previous
        {
            !obj.is_none()
                && obj.eq(&object)?
                && maximum.checked_add(1) == Some(index)
                && previous_font.eq(&font)?
                && *previous_rotation == rotation
                && previous_origin
                    .zip(origin)
                    .is_some_and(|(a, b)| a.iter().zip(b).all(|(x, y)| (x - y).abs() <= 0.001))
                && previous_box
                    .iter()
                    .zip(box4)
                    .all(|(x, y)| (x - y).abs() <= 0.001)
                && *previous_text
                && has_text
        } else {
            false
        };
        if same {
            // 不提前物化异常汉字映射或连字，保留原 Unicode 转换及来源合并顺序。
            if !head_text.as_ref().expect("mapping head exists").eq(&text)? {
                return Ok(None);
            }
            output.last_mut().expect("mapping group exists").1 = position + 1;
        } else {
            let box_tuple = if box4[2] > box4[0] && box4[3] > box4[1] {
                Some(PyTuple::new(
                    py,
                    (0..4)
                        .map(|i| raw.get_item(i))
                        .collect::<PyResult<Vec<_>>>()?,
                )?)
            } else {
                None
            };
            output.push((position, position + 1, box_tuple, angle));
            head_text = Some(text);
        }
        previous = Some((object, last_source, font, rotation, origin, box4, has_text));
    }
    Ok(Some(output))
}

/// 批量生成重复绘制候选，不在内层循环回调 Python。
#[pyfunction]
pub(super) fn paint_pairs(
    py: Python<'_>,
    records: Vec<dedup::PaintRecord>,
) -> Option<(Vec<dedup::Pair>, Vec<dedup::Offset>)> {
    py.detach(move || dedup::paint_pairs(records))
}

/// 检查来源索引后批量计算最早来源连通关系。
#[pyfunction]
pub(super) fn dedup_components(
    py: Python<'_>,
    count: usize,
    pairs: Vec<dedup::Pair>,
) -> PyResult<Vec<usize>> {
    if pairs.iter().any(|(a, b)| *a >= count || *b >= count) {
        return Err(pyo3::exceptions::PyIndexError::new_err(
            "glyph index out of range",
        ));
    }
    Ok(py.detach(move || dedup::components(count, &pairs)))
}

/// 返回隐藏 OCR 的几何候选，文本比较与来源物化仍由 Python 拥有。
#[pyfunction]
pub(super) fn hidden_candidates(
    py: Python<'_>,
    records: Vec<dedup::HiddenRecord>,
) -> Option<Vec<(Vec<usize>, Vec<usize>)>> {
    py.detach(move || dedup::hidden_candidates(records))
}

/// 在原始候选顺序下确认平移证据，并验证全部来源索引。
#[pyfunction]
pub(super) fn confirmed_offsets(
    py: Python<'_>,
    records: Vec<dedup::EvidenceRecord>,
    pairs: Vec<dedup::Offset>,
    exact: Vec<dedup::Pair>,
) -> PyResult<Vec<dedup::Pair>> {
    if pairs
        .iter()
        .any(|p| p.0 >= records.len() || p.1 >= records.len())
        || exact
            .iter()
            .any(|(a, b)| *a >= records.len() || *b >= records.len())
    {
        return Err(pyo3::exceptions::PyIndexError::new_err(
            "glyph index out of range",
        ));
    }
    Ok(py.detach(move || dedup::confirmed_offsets(records, pairs, exact)))
}

/// 只传递一次每字符数值，返回分组范围以复用原有字符对象。
#[pyfunction]
pub(super) fn mapping_runs(
    py: Python<'_>,
    records: Vec<dedup::MappingRecord>,
) -> Option<Vec<(usize, usize)>> {
    py.detach(move || dedup::mapping_runs(records))
}
