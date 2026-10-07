//! 标题阶段上下文的普通对象验证及只读尺度快照，不跨阶段缓存语义或成员。
// 高频字段名按解释器复用不可变字符串，仍由原 Python 属性和字典操作读取当前状态。
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PySet, PyString, PyTuple};

/// 保持原阶段缓存的数值范围及类型条件，巨大整数不调用浮点转换。
fn number(value: &Bound<'_, PyAny>) -> Option<f64> {
    if value.is_exact_instance_of::<PyFloat>() {
        let value = value.extract::<f64>().ok()?;
        value.is_finite().then_some(value)
    } else if value.is_exact_instance_of::<PyInt>() {
        let value = value.extract::<i64>().ok()?;
        (-(1_i64 << 53)..=1_i64 << 53)
            .contains(&value)
            .then_some(value as f64)
    } else {
        None
    }
}

/// 先验证全部普通行，再建立原身份字典；未知字段由原上下文重新检查并保持回调行为。
#[pyfunction]
pub(super) fn lane_profile_context_owned<'py>(
    py: Python<'py>,
    lanes: &Bound<'py, PyAny>,
    lane_type: &Bound<'py, PyAny>,
    line_type: &Bound<'py, PyAny>,
) -> PyResult<Option<(Bound<'py, PyDict>, Bound<'py, PyDict>)>> {
    if !lanes.is_exact_instance_of::<PyList>() {
        return Ok(None);
    }
    let mut records = Vec::new();
    for lane in lanes.try_iter()? {
        let lane = lane?;
        if !lane.get_type().is(lane_type)
            || number(&lane.getattr(pyo3::intern!(lane.py(), "left"))?).is_none()
            || number(&lane.getattr(pyo3::intern!(lane.py(), "right"))?).is_none()
        {
            return Ok(None);
        }
        let rows = lane.getattr(pyo3::intern!(lane.py(), "lines"))?;
        if !rows.is_exact_instance_of::<PyList>() {
            return Ok(None);
        }
        for row in rows.try_iter()? {
            let row = row?;
            if !row.is_exact_instance_of::<PyTuple>() || row.len()? != 2 {
                return Ok(None);
            }
            let line = row.get_item(0)?;
            let bbox = row.get_item(1)?;
            if !line.get_type().is(line_type)
                || !line
                    .getattr(pyo3::intern!(line.py(), "text"))?
                    .is_exact_instance_of::<PyString>()
                || !line
                    .getattr(pyo3::intern!(line.py(), "angle"))?
                    .is_exact_instance_of::<PyInt>()
                || (!bbox.is_exact_instance_of::<PyList>()
                    && !bbox.is_exact_instance_of::<PyTuple>())
                || bbox.len()? != 4
            {
                return Ok(None);
            }
            for axis in 0..4 {
                if number(&bbox.get_item(axis)?).is_none() {
                    return Ok(None);
                }
            }
            let em = line.getattr(pyo3::intern!(line.py(), "em_height"))?;
            let height = line.getattr(pyo3::intern!(line.py(), "effective_height"))?;
            if number(&em).is_none()
                || number(&height).is_none()
                || number(&line.getattr(pyo3::intern!(line.py(), "font_coverage"))?).is_none()
            {
                return Ok(None);
            }
            let weight = line.getattr(pyo3::intern!(line.py(), "dominant_font_weight"))?;
            let semantic = line.getattr(pyo3::intern!(line.py(), "semantic_type"))?;
            let visual = line.getattr(pyo3::intern!(line.py(), "visual_row_id"))?;
            if (!weight.is_none() && number(&weight).is_none())
                || (!semantic.is_none() && !semantic.is_exact_instance_of::<PyString>())
                || (!visual.is_none() && !visual.is_exact_instance_of::<PyInt>())
                || !line
                    .getattr(pyo3::intern!(line.py(), "source_index"))?
                    .is_exact_instance_of::<PyInt>()
            {
                return Ok(None);
            }
            let repaired = line.getattr(pyo3::intern!(line.py(), "style_scale_repaired"))?;
            if !repaired.is_exact_instance_of::<PyBool>()
                || !line
                    .getattr(pyo3::intern!(line.py(), "restored_inline_cluster"))?
                    .is_exact_instance_of::<PyBool>()
                || !line
                    .getattr(pyo3::intern!(line.py(), "split_from_row"))?
                    .is_exact_instance_of::<PyBool>()
            {
                return Ok(None);
            }
            let font = line.getattr(pyo3::intern!(line.py(), "font_signature"))?;
            if !font.is_none()
                && (!font.is_exact_instance_of::<PyTuple>()
                    || font.len()? != 2
                    || !font.get_item(0)?.is_exact_instance_of::<PyString>()
                    || !font.get_item(1)?.is_exact_instance_of::<PyInt>())
            {
                return Ok(None);
            }
            records.push((
                lane.as_ptr() as usize,
                line.as_ptr() as usize,
                bbox,
                em,
                height,
                repaired.extract::<bool>()?,
            ));
        }
    }
    let heights = PyDict::new(py);
    let memberships = PyDict::new(py);
    for (lane, line, bbox, em, height, repaired) in records {
        let value = if repaired && number(&em).unwrap() > 0.0 {
            em
        } else if number(&height).unwrap() != 0.0 {
            height
        } else {
            let bottom = bbox.get_item(3)?;
            let top = bbox.get_item(1)?;
            // 安全：持有 GIL 和坐标强引用；普通 int/float 的原生减法保留整数结果及其精度。
            unsafe {
                Bound::from_owned_ptr_or_err(
                    py,
                    pyo3::ffi::PyNumber_Subtract(bottom.as_ptr(), top.as_ptr()),
                )?
            }
        };
        let value = if value.extract::<f64>()? > 0.1 {
            value
        } else {
            0.1_f64.into_pyobject(py)?.into_any()
        };
        heights.set_item((line, bbox.as_ptr() as usize), value)?;
        if let Some(existing) = memberships.get_item(line)? {
            existing.cast::<PySet>()?.add(lane)?;
        } else {
            let members = PySet::empty(py)?;
            members.add(lane)?;
            memberships.set_item(line, members)?;
        }
    }
    Ok(Some((heights, memberships)))
}
