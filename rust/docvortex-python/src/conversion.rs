//! 绑定模块共用的框读取与 Python 四元组转换，不承载计算决策。

use docvortex_core::geometry::Box4;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

/// 持有 GIL 和字典强引用时只读借用键指针，保留完整类型校验且不逐键增减引用。
pub(super) fn plain_string_keys(value: &Bound<'_, PyDict>) -> bool {
    let mut position = 0;
    let mut key = std::ptr::null_mut();
    // 安全：调用方提供普通字典，循环内没有 Python 回调或 GIL 释放，字典不会被修改。
    unsafe {
        while pyo3::ffi::PyDict_Next(
            value.as_ptr(),
            &mut position,
            &mut key,
            std::ptr::null_mut(),
        ) != 0
        {
            if pyo3::ffi::PyUnicode_CheckExact(key) == 0 {
                return false;
            }
        }
    }
    true
}

/// 源索引字典只接受普通整数键，逐键借用检查避免分配列表及自定义比较回调。
pub(super) fn plain_integer_keys(value: &Bound<'_, PyDict>) -> bool {
    let mut position = 0;
    let mut key = std::ptr::null_mut();
    // 安全：完整遍历期间持有 GIL 与字典强引用，不调用 Python 或修改字典。
    unsafe {
        while pyo3::ffi::PyDict_Next(
            value.as_ptr(),
            &mut position,
            &mut key,
            std::ptr::null_mut(),
        ) != 0
        {
            if pyo3::ffi::PyLong_CheckExact(key) == 0 {
                return false;
            }
        }
    }
    true
}

pub(super) type BoxTuple = (f64, f64, f64, f64);

/// 在 Python 绑定层将框数组物化为原有不可变四元组。
pub(super) fn box_tuple(b: Box4) -> BoxTuple {
    (b[0], b[1], b[2], b[3])
}

/// 普通数值直接提取；特殊可迭代输入仍使用原 Python 校验语义。
pub(super) fn read_boxes(
    values: &Bound<'_, PyList>,
    fallback: &Bound<'_, PyAny>,
) -> PyResult<Vec<Option<Box4>>> {
    values
        .iter()
        .map(|v| match v.extract::<Option<Box4>>() {
            Ok(b) => Ok(b),
            Err(_) => fallback.call1((v,))?.extract::<Option<Box4>>(),
        })
        .collect()
}
