//! 批量物化普通冻结投影字符，保持 Python 类型、Unicode 特征及全部源偏移。
use pyo3::prelude::*;
use pyo3::types::{PyFrozenSet, PyList, PyString};
use std::collections::HashMap;

/// 普通字符串才能批量投影；孤立代理字符和未知值使用完整 Python 参考路径。
#[pyfunction]
pub(super) fn project_content_chars_owned<'py>(
    py: Python<'py>,
    content: &Bound<'py, PyAny>,
    normalizer: &Bound<'py, PyAny>,
    record_type: &Bound<'py, PyAny>,
    allocator: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyList>>> {
    if !content.is_exact_instance_of::<PyString>() {
        return Ok(None);
    }
    let Ok(content) = content.extract::<String>() else {
        return Ok(None);
    };
    let tokens = py.detach(|| docvortex_core::text_projection::visible_tokens(&content));
    let mut forms = HashMap::new();
    for &(_, value, _) in &tokens {
        if let std::collections::hash_map::Entry::Vacant(entry) = forms.entry(value) {
            let value = normalizer.call1((value.to_string(),))?;
            let Ok(value) = value.extract::<String>() else {
                return Ok(None);
            };
            entry.insert(value);
        }
    }
    let projected = py.detach(|| docvortex_core::text_projection::project_tokens(&tokens, &forms));
    let keys: Vec<_> = [
        "value",
        "raw_start",
        "raw_end",
        "existing_styles",
        "formula_gap_before",
        "inside_hyperlink",
    ]
    .into_iter()
    .map(|key| PyString::new(py, key))
    .collect();
    let styles = PyFrozenSet::empty(py)?;
    let output = PyList::empty(py);
    let mut values = HashMap::new();
    for (value, start, end, gap) in projected {
        let value = values
            .entry(value)
            .or_insert_with(|| PyString::new(py, &value.to_string()));
        let record = allocator.call1((record_type,))?;
        let fields: [Bound<'py, PyAny>; 6] = [
            value.clone().into_any(),
            start.into_pyobject(py)?.into_any(),
            end.into_pyobject(py)?.into_any(),
            styles.clone().into_any(),
            gap.into_pyobject(py)?.to_owned().into_any(),
            false.into_pyobject(py)?.to_owned().into_any(),
        ];
        for (key, field) in keys.iter().zip(fields) {
            set_frozen_slot(py, &record, key, &field)?;
        }
        output.append(record)?;
    }
    Ok(Some(output))
}

/// 沿用冻结 dataclass 初始化时 object.__setattr__ 的通用槽写入，不调用冻结对象的赋值拦截器。
pub(super) fn set_frozen_slot(
    py: Python<'_>,
    record: &Bound<'_, PyAny>,
    key: &Bound<'_, PyString>,
    value: &Bound<'_, PyAny>,
) -> PyResult<()> {
    // 安全：调用时持有 GIL，三个参数均由 Bound 强引用保持存活；C API 返回错误后立即提取异常。
    let result = unsafe {
        pyo3::ffi::PyObject_GenericSetAttr(record.as_ptr(), key.as_ptr(), value.as_ptr())
    };
    if result == 0 {
        Ok(())
    } else {
        Err(PyErr::fetch(py))
    }
}
