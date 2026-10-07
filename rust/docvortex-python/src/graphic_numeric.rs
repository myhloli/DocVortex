//! 数字图形文字批量筛选，只复用相同文字状态，不缓存几何或最终图体。
use pyo3::prelude::*;
use pyo3::types::{PyList, PyString, PyTuple};

const PATTERNS: [&str; 3] = [
    r"[+−-]?\d+(?:\.\d+)?",
    r"\s*[+−-]?\d+(?:[,.]\d+)?%?\s*",
    r"\s*[-+]?\d+(?:[,.]\d+)*%?\s*",
];

/// ASCII 空白与 Python 的 strip 和正则空白保持一致，包含四个信息分隔符。
fn ascii_space(value: u8) -> bool {
    matches!(value, 9..=13 | 28..=32)
}

/// 逐字节执行三种既有数字语法，不使用不同 Unicode 版本的 Rust 字符分类。
fn ascii_numeric(text: &str, mode: usize) -> bool {
    let bytes = text.as_bytes();
    let mut start = 0;
    let mut end = bytes.len();
    while start < end && ascii_space(bytes[start]) {
        start += 1;
    }
    while end > start && ascii_space(bytes[end - 1]) {
        end -= 1;
    }
    if mode == 0 && end - start > 8 {
        return false;
    }
    let mut pos = start;
    if pos < end && matches!(bytes[pos], b'+' | b'-') {
        pos += 1;
    }
    let digits = pos;
    while pos < end && bytes[pos].is_ascii_digit() {
        pos += 1;
    }
    if pos == digits {
        return false;
    }
    let mut separators = 0;
    while pos < end && (bytes[pos] == b'.' || mode != 0 && bytes[pos] == b',') {
        if separators > 0 && mode != 2 {
            return false;
        }
        separators += 1;
        pos += 1;
        let digits = pos;
        while pos < end && bytes[pos].is_ascii_digit() {
            pos += 1;
        }
        if pos == digits {
            return false;
        }
    }
    if mode != 0 && pos < end && bytes[pos] == b'%' {
        pos += 1;
    }
    pos == end
}

/// 返回稳定顺序的原行对象，非 ASCII 文字沿用 Python 正则，未知对象完整回退。
#[pyfunction]
pub(super) fn graphic_numeric_lines<'py>(
    py: Python<'py>,
    lines: &Bound<'py, PyAny>,
    mode: usize,
    line_type: &Bound<'py, PyAny>,
    matcher: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyList>>> {
    let Ok(lines) = lines.cast_exact::<PyList>() else {
        return Ok(None);
    };
    if mode > 2 {
        return Ok(None);
    }
    // 在任何缓存写入前验证完整输入，避免未知对象触发额外属性回调。
    for line in lines.iter() {
        if !line.get_type().is(line_type)
            || !line
                .getattr(pyo3::intern!(py, "text"))?
                .is_exact_instance_of::<PyString>()
        {
            return Ok(None);
        }
    }
    let output = PyList::empty(py);
    for line in lines.iter() {
        let text = line.getattr(pyo3::intern!(py, "text"))?;
        let cache = line.getattr(pyo3::intern!(py, "_graphic_numeric_features"))?;
        let mut known = 0u8;
        let mut matched = 0u8;
        if let Ok(cache) = cache.cast_exact::<PyTuple>() {
            if cache.len() == 3 && cache.get_item(0)?.is(&text) {
                if let (Ok(a), Ok(b)) = (
                    cache.get_item(1)?.extract::<u8>(),
                    cache.get_item(2)?.extract::<u8>(),
                ) {
                    known = a;
                    matched = b;
                }
            }
        }
        let bit = 1 << mode;
        if known & bit == 0 {
            let string = text.cast::<PyString>()?;
            let Ok(raw) = string.to_str() else {
                return Ok(None);
            };
            if raw.is_ascii() {
                for kind in 0..3 {
                    if ascii_numeric(raw, kind) {
                        matched |= 1 << kind;
                    }
                }
                known = 7;
            } else {
                let value = if mode == 0 {
                    text.call_method0(pyo3::intern!(py, "strip"))?
                } else {
                    text.clone()
                };
                if matcher.call1((PATTERNS[mode], &value))?.is_truthy()?
                    && (mode != 0 || value.len()? <= 8)
                {
                    matched |= bit;
                }
                known |= bit;
            }
            line.setattr(
                pyo3::intern!(py, "_graphic_numeric_features"),
                (&text, known, matched),
            )?;
        }
        if matched & bit != 0 {
            output.append(line)?;
        }
    }
    Ok(Some(output))
}
