//! 绑定模块共用的框读取与 Python 四元组转换，不承载计算决策。

use docvortex_core::geometry::Box4;
use pyo3::prelude::*;
use pyo3::types::PyList;

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
