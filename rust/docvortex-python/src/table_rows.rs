//! 表格片段一次读取、分组及物化；原对象、Python 求和与坐标类型均保持不变。
// 高频字段名按解释器复用不可变字符串，仍由原 Python 属性和字典操作读取当前状态。
use pyo3::prelude::*;
use pyo3::types::{PyFloat, PyInt, PyList, PyTuple};

/// 普通有限坐标才进入批量路径，特殊数值完整交回 Python 处理。
fn number(value: &Bound<'_, PyAny>) -> Option<f64> {
    if !value.is_exact_instance_of::<PyFloat>() && !value.is_exact_instance_of::<PyInt>() {
        return None;
    }
    let value = value.extract::<f64>().ok()?;
    (value.is_finite() && value.abs() <= 2f64.powi(50)).then_some(value)
}

/// 完整验证后才调用原有均值、求和及行构造函数；不跨调用缓存成员或字段。
#[pyfunction]
pub(super) fn fragment_rows_owned<'py>(
    py: Python<'py>,
    fragments: &Bound<'py, PyAny>,
    height: &Bound<'py, PyAny>,
    fragment_type: &Bound<'py, PyAny>,
    row_type: &Bound<'py, PyAny>,
    mean: &Bound<'py, PyAny>,
    total: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyList>>> {
    if !fragments.is_exact_instance_of::<PyList>() {
        return Ok(None);
    }
    let Some(height) = number(height) else {
        return Ok(None);
    };
    let mut references = Vec::new();
    let mut boxes = Vec::new();
    let mut original_boxes = Vec::new();
    let mut coordinates = Vec::new();
    let mut items = Vec::new();
    for fragment in fragments.try_iter()? {
        let fragment = fragment?;
        if !fragment.get_type().is(fragment_type) {
            return Ok(None);
        }
        let box_obj = fragment.getattr(pyo3::intern!(fragment.py(), "local_bbox"))?;
        if (!box_obj.is_exact_instance_of::<PyTuple>() && !box_obj.is_exact_instance_of::<PyList>())
            || box_obj.len()? != 4
        {
            return Ok(None);
        }
        let mut box_values = Vec::new();
        let mut bbox = [0.0; 4];
        for (i, value) in bbox.iter_mut().enumerate() {
            let coordinate = box_obj.get_item(i)?;
            let Some(number) = number(&coordinate) else {
                return Ok(None);
            };
            *value = number;
            box_values.push(coordinate);
        }
        let id = fragment.getattr(pyo3::intern!(fragment.py(), "visual_row_id"))?;
        let id = if id.is_none() {
            None
        } else if id.is_exact_instance_of::<PyInt>() {
            let Ok(id) = id.extract::<i64>() else {
                return Ok(None);
            };
            Some(id)
        } else {
            return Ok(None);
        };
        items.push(((bbox[1] + bbox[3]) / 2.0, bbox[0], id));
        references.push(fragment);
        boxes.push(bbox);
        coordinates.push(box_values);
        original_boxes.push(box_obj);
    }
    let mut groups = docvortex_core::rule_text::fragment_row_groups(
        &items,
        2.0_f64.max(height * 0.5),
        |indices| -> PyResult<f64> {
            if indices.len() == 1 {
                let value = items[indices[0]].0;
                Ok(if value == 0.0 { 0.0 } else { value })
            } else {
                mean.call1((indices.iter().map(|&i| items[i].0).collect::<Vec<_>>(),))?
                    .extract::<f64>()
            }
        },
    )?;
    let mut rows = Vec::new();
    for group in &mut groups {
        group.sort_by(|&a, &b| items[a].1.partial_cmp(&items[b].1).unwrap());
        let members = PyList::new(py, group.iter().map(|&i| references[i].clone()))?;
        let sum = total.call1((group.iter().map(|&i| items[i].0).collect::<Vec<_>>(),))?;
        let center = sum.extract::<f64>()? / group.len() as f64;
        let mut chosen = [group[0]; 4];
        for &i in &group[1..] {
            for axis in 0..4 {
                // 比较相等时保留先出现的原对象，包含整数坐标类型及正负零。
                if (axis < 2 && boxes[i][axis] < boxes[chosen[axis]][axis])
                    || (axis >= 2 && boxes[i][axis] > boxes[chosen[axis]][axis])
                {
                    chosen[axis] = i;
                }
            }
        }
        let bbox = if group.len() == 1 {
            original_boxes[group[0]].clone()
        } else {
            PyTuple::new(
                py,
                (0..4).map(|axis| coordinates[chosen[axis]][axis].clone()),
            )?
            .into_any()
        };
        let mut visual = None;
        let mut different = false;
        for &i in group.iter() {
            if let Some(id) = items[i].2 {
                if visual.is_some_and(|first| first != id) {
                    different = true;
                }
                visual = Some(id);
            }
        }
        let row = row_type.call1((members, center, bbox, if different { None } else { visual }))?;
        rows.push((center, row));
    }
    rows.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());
    Ok(Some(PyList::new(py, rows.into_iter().map(|(_, row)| row))?))
}
