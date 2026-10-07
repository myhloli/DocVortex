//! 同一次候选构建持有填充框的自有快照；每个区间独立执行完整几何裁决。
use pyo3::prelude::*;
use pyo3::types::{PyFloat, PyList, PyTuple};

/// 只接受普通有限浮点，特殊数值和用户定义转换完整回退。
fn number(value: &Bound<'_, PyAny>) -> Option<f64> {
    if !value.is_exact_instance_of::<PyFloat>() {
        return None;
    }
    let value = value.extract::<f64>().ok()?;
    (value.is_finite() && value.abs() <= 1e100).then_some(value)
}

/// 对当前查询读取完整普通框，不保留调用方可变框对象。
fn bbox(value: &Bound<'_, PyAny>) -> Option<[f64; 4]> {
    if !value.is_exact_instance_of::<PyTuple>() || value.len().ok()? != 4 {
        return None;
    }
    let mut output = [0.0; 4];
    for (i, axis) in output.iter_mut().enumerate() {
        *axis = number(&value.get_item(i).ok()?)?;
    }
    Some(output)
}

/// 仅保存检测器独占构建期间的填充框，不读取 PDFium 或缓存任何最终候选结果。
#[pyclass]
pub(super) struct NativeFillBands {
    boxes: Vec<[f64; 4]>,
}

#[pymethods]
impl NativeFillBands {
    /// 构建时复制数值框；调用方后续变更须重新创建页面快照。
    #[new]
    fn new(boxes: Vec<[f64; 4]>) -> Self {
        Self { boxes }
    }

    /// 每个查询独立保留边界、重叠去重和首个分组语义，纯自有计算释放 GIL。
    fn count(
        &self,
        py: Python<'_>,
        rule: &Bound<'_, PyAny>,
        em: &Bound<'_, PyAny>,
    ) -> Option<usize> {
        let rule = bbox(rule)?;
        let em = number(em)?;
        if self
            .boxes
            .iter()
            .flatten()
            .any(|v| !v.is_finite() || v.abs() > 1e100)
        {
            return None;
        }
        Some(py.detach(|| {
            docvortex_core::table_candidates::repeated_fill_bands(&self.boxes, rule, em)
        }))
    }
}

/// 普通列表和元组的完整当前状态在构建边界检查，未知输入不启动自有快照。
#[pyfunction]
pub(super) fn prepare_fill_bands(boxes: &Bound<'_, PyAny>) -> Option<NativeFillBands> {
    let Ok(boxes) = boxes.cast_exact::<PyList>() else {
        return None;
    };
    let values = boxes
        .iter()
        .map(|value| bbox(&value))
        .collect::<Option<Vec<_>>>()?;
    Some(NativeFillBands { boxes: values })
}
