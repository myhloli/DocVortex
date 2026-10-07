//! 合并行的排版统计直接读取普通字形记录，只在本次调用复用字体与 Unicode 分类。
use docvortex_core::{geometry, statistics};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use std::collections::HashMap;

type OwnedTypography<'py> = (statistics::Typography, Option<Bound<'py, PyTuple>>);

/// 仅解包普通坐标和自有 Bbox，无法无副作用转换的输入交回完整参考路径。
fn plain_box(value: &Bound<'_, PyAny>, bbox_type: &Bound<'_, PyAny>) -> Option<Option<[f64; 4]>> {
    if value.is_none() {
        return Some(None);
    }
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
    let mut b = [0.0; 4];
    for (i, number) in b.iter_mut().enumerate() {
        let value = raw.get_item(i).ok()?;
        if value.is_exact_instance_of::<PyFloat>() {
            *number = value.extract().ok()?;
        } else if value.is_exact_instance_of::<PyInt>() || value.is_exact_instance_of::<PyBool>() {
            let integer = value.extract::<i64>().ok()?;
            if integer.unsigned_abs() > 1 << 53 {
                return None;
            }
            *number = integer as f64;
        } else {
            return None;
        }
    }
    Some(Some(b))
}

/// 标志只接受普通整数；非标准转换和超大整数由 Python 保留原 int 语义。
fn plain_flags(value: Option<Bound<'_, PyAny>>) -> Option<i64> {
    let Some(value) = value else { return Some(0) };
    if value.is_none() {
        return Some(0);
    }
    if !value.is_exact_instance_of::<PyInt>() && !value.is_exact_instance_of::<PyBool>() {
        return None;
    }
    value.extract().ok()
}

/// 字重接受普通有限数值，缺失与非正值沿用无证据状态。
fn plain_weight(value: Option<Bound<'_, PyAny>>) -> Option<Option<f64>> {
    let Some(value) = value else {
        return Some(None);
    };
    if value.is_none() {
        return Some(None);
    }
    if !value.is_exact_instance_of::<PyFloat>()
        && !value.is_exact_instance_of::<PyInt>()
        && !value.is_exact_instance_of::<PyBool>()
    {
        return None;
    }
    let number = value.extract::<f64>().ok()?;
    Some((number.is_finite() && number > 0.0).then_some(number))
}

/// 逐行读取当前状态并聚合自有数值；字体族仍由原 Python 规则计算，结果不跨行缓存。
#[pyfunction]
pub(super) fn typography_owned<'py>(
    py: Python<'py>,
    line: &Bound<'py, PyAny>,
    size: geometry::Size,
    line_type: &Bound<'_, PyAny>,
    bbox_type: &Bound<'_, PyAny>,
    glyph_flags: &Bound<'_, PyAny>,
    family_name: &Bound<'_, PyAny>,
) -> PyResult<Option<OwnedTypography<'py>>> {
    if !line.get_type().is(line_type) || size.iter().any(|v| !v.is_finite()) {
        return Ok(None);
    }
    let raw_angle = line.getattr(pyo3::intern!(py, "angle"))?;
    if !raw_angle.is_exact_instance_of::<PyInt>() && !raw_angle.is_exact_instance_of::<PyFloat>() {
        return Ok(None);
    }
    let angle = raw_angle.extract::<f64>()?;
    if ![0.0, 90.0, 180.0, 270.0].contains(&angle) {
        return Ok(None);
    }
    let Some(Some(line_box)) = plain_box(&line.getattr(pyo3::intern!(py, "bbox"))?, bbox_type)
    else {
        return Ok(None);
    };
    let local = geometry::rotate(line_box, size, angle as i32);
    let fallback_height = if local[3] - local[1] > 0.1 {
        local[3] - local[1]
    } else {
        0.1
    };
    let raw_chars = line.getattr(pyo3::intern!(py, "chars"))?;
    let Ok(chars) = raw_chars.cast_exact::<PyList>() else {
        return Ok(None);
    };
    let mut boxes = Vec::with_capacity(chars.len());
    let mut fonts = Vec::with_capacity(chars.len());
    let mut weights = Vec::with_capacity(chars.len());
    let mut signatures = Vec::new();
    let mut signature_objects = Vec::new();
    let mut signature_ids = HashMap::new();
    let mut font_cache = HashMap::new();
    let mut text_cache = HashMap::new();
    for char in chars.iter() {
        let Ok(char) = char.cast_exact::<PyDict>() else {
            return Ok(None);
        };
        if !super::conversion::plain_string_keys(char) {
            return Ok(None);
        }
        let text = match char.get_item(pyo3::intern!(py, "char"))? {
            None => PyString::new(py, ""),
            Some(text) if text.is_none() => PyString::new(py, ""),
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
        let Ok(raw_text) = text.to_str() else {
            return Ok(None);
        };
        let keep = if raw_text.is_ascii() {
            // Python 空字符串 isprintable 为真、isspace 为假，不能把空字形从统计中移除。
            raw_text.bytes().all(|v| (32..=126).contains(&v))
                && (raw_text.is_empty() || raw_text.bytes().any(|v| v != 32))
        } else if let Some(keep) = text_cache.get(raw_text) {
            *keep
        } else {
            let keep = glyph_flags.call1((&text,))?.extract::<u8>()? & 1 != 0;
            text_cache.insert(raw_text.to_owned(), keep);
            keep
        };
        let bbox = if keep {
            let raw = char
                .get_item(pyo3::intern!(py, "bbox"))?
                .unwrap_or_else(|| py.None().into_bound(py));
            let Some(bbox) = plain_box(&raw, bbox_type) else {
                return Ok(None);
            };
            geometry::clip(geometry::normalize(bbox, false), size)
                .map(|b| geometry::rotate(b, size, angle as i32))
        } else {
            None
        };
        boxes.push(bbox);
        if bbox.is_none() {
            fonts.push(None);
            weights.push(None);
            continue;
        }
        let font = char.get_item(pyo3::intern!(py, "font"))?;
        let (identifier, weight) = if let Some(font) = font.filter(|v| !v.is_none()) {
            let Ok(font) = font.cast_into::<PyDict>() else {
                return Ok(None);
            };
            if !font.is_exact_instance_of::<PyDict>() {
                return Ok(None);
            }
            let key = font.as_ptr() as usize;
            if let Some((_, identifier, weight)) = font_cache.get(&key) {
                (*identifier, *weight)
            } else {
                if !super::conversion::plain_string_keys(&font) {
                    return Ok(None);
                }
                let raw_name = font.get_item(pyo3::intern!(py, "name"))?;
                let name = match raw_name.as_ref() {
                    None => String::new(),
                    Some(name) if name.is_none() => String::new(),
                    Some(name) => {
                        let Ok(name) = name.cast_exact::<PyString>() else {
                            return Ok(None);
                        };
                        let Ok(name) = name.to_str() else {
                            return Ok(None);
                        };
                        name.to_owned()
                    }
                };
                let (identifier, weight) = if name.is_empty() {
                    (None, None)
                } else {
                    let Some(flags) = plain_flags(font.get_item(pyo3::intern!(py, "flags"))?)
                    else {
                        return Ok(None);
                    };
                    let signature = (name, flags);
                    let identifier = if let Some(index) = signature_ids.get(&signature) {
                        *index
                    } else {
                        let index = signatures.len();
                        let raw_flags = font.get_item(pyo3::intern!(py, "flags"))?;
                        let flag_object = match raw_flags {
                            Some(value) if value.is_exact_instance_of::<PyInt>() => value,
                            _ => flags.into_pyobject(py)?.into_any(),
                        };
                        signature_objects.push(PyTuple::new(py, [raw_name.unwrap(), flag_object])?);
                        signature_ids.insert(signature.clone(), index);
                        signatures.push(signature);
                        index
                    };
                    let Some(weight) = plain_weight(font.get_item(pyo3::intern!(py, "weight"))?)
                    else {
                        return Ok(None);
                    };
                    (Some(identifier), weight)
                };
                font_cache.insert(key, (font, identifier, weight));
                (identifier, weight)
            }
        } else {
            (None, None)
        };
        fonts.push(identifier);
        weights.push(weight);
    }
    let mut families = Vec::with_capacity(signatures.len());
    let mut family_ids = HashMap::new();
    for signature in &signatures {
        let family = family_name.call1((signature.clone(),))?;
        families.push(if family.is_none() {
            None
        } else {
            let name = family.extract::<String>()?;
            let next = family_ids.len();
            Some(*family_ids.entry(name).or_insert(next))
        });
    }
    let result = py
        .detach(move || statistics::typography(boxes, fonts, weights, &families, fallback_height));
    Ok(result.map(|metrics| {
        let signature = metrics.2.map(|winner| signature_objects[winner].clone());
        (metrics, signature)
    }))
}
