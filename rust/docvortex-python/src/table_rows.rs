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
) -> PyResult<Bound<'py, PyList>> {
    let generic = || fragment_rows_generic(py, fragments, height, row_type, mean, total);
    if !fragments.is_exact_instance_of::<PyList>() {
        return generic();
    }
    let Some(height) = number(height) else {
        return generic();
    };
    let mut references = Vec::new();
    let mut boxes = Vec::new();
    let mut original_boxes = Vec::new();
    let mut coordinates = Vec::new();
    let mut items = Vec::new();
    for fragment in fragments.try_iter()? {
        let fragment = fragment?;
        if !fragment.get_type().is(fragment_type) {
            return generic();
        }
        let box_obj = fragment.getattr(pyo3::intern!(fragment.py(), "local_bbox"))?;
        if (!box_obj.is_exact_instance_of::<PyTuple>() && !box_obj.is_exact_instance_of::<PyList>())
            || box_obj.len()? != 4
        {
            return generic();
        }
        let mut box_values = Vec::new();
        let mut bbox = [0.0; 4];
        for (i, value) in bbox.iter_mut().enumerate() {
            let coordinate = box_obj.get_item(i)?;
            let Some(number) = number(&coordinate) else {
                return generic();
            };
            *value = number;
            box_values.push(coordinate);
        }
        let id = fragment.getattr(pyo3::intern!(fragment.py(), "visual_row_id"))?;
        let id = if id.is_none() {
            None
        } else if id.is_exact_instance_of::<PyInt>() {
            let Ok(id) = id.extract::<i64>() else {
                return generic();
            };
            Some(id)
        } else {
            return generic();
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
    PyList::new(py, rows.into_iter().map(|(_, row)| row))
}

/// 通用片段仍在 Rust 执行分组，宿主 fmean/sum 和原坐标对象维持特殊数值及构造语义。
fn fragment_rows_generic<'py>(
    py: Python<'py>,
    fragments: &Bound<'py, PyAny>,
    height: &Bound<'py, PyAny>,
    row_type: &Bound<'py, PyAny>,
    mean: &Bound<'py, PyAny>,
    total: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyList>> {
    use pyo3::types::{PyDict, PySet};
    let operators = py.import("operator")?;
    let builtins = py.import("builtins")?;
    let add = operators.getattr("add")?;
    let sub = operators.getattr("sub")?;
    let multiply = operators.getattr("mul")?;
    let divide = operators.getattr("truediv")?;
    let absolute = operators.getattr("abs")?;
    let le = operators.getattr("le")?;
    let minimum = builtins.getattr("min")?;
    let maximum = builtins.getattr("max")?;
    let tolerance = maximum.call1((2.0, multiply.call1((height, 0.5))?))?;
    let mut references = Vec::new();
    let mut boxes = Vec::new();
    let mut centers = Vec::new();
    let mut visual = Vec::new();
    let mut seeds: Vec<Vec<usize>> = Vec::new();
    let mut geometric = Vec::new();
    let native = PyDict::new(py);
    for fragment in fragments.try_iter()? {
        let fragment = fragment?;
        let bbox = fragment.getattr("local_bbox")?;
        let center = divide.call1((add.call1((bbox.get_item(1)?, bbox.get_item(3)?))?, 2.0))?;
        let id = fragment.getattr("visual_row_id")?;
        let index = references.len();
        if id.is_none() {
            geometric.push(vec![index]);
        } else if let Some(group) = native.get_item(&id)? {
            seeds[group.extract::<usize>()?].push(index);
        } else {
            native.set_item(&id, seeds.len())?;
            seeds.push(vec![index]);
        }
        references.push(fragment);
        boxes.push(bbox);
        centers.push(center);
        visual.push(id);
    }
    seeds.extend(geometric);
    let mut seed_centers = Vec::new();
    let keys = PyList::empty(py);
    for group in &seeds {
        let center = mean.call1((PyList::new(py, group.iter().map(|&i| centers[i].clone()))?,))?;
        let left = minimum.call1((PyList::new(
            py,
            group
                .iter()
                .map(|&i| boxes[i].get_item(0))
                .collect::<PyResult<Vec<_>>>()?,
        )?,))?;
        keys.append((&center, left))?;
        seed_centers.push(center);
    }
    let mut groups: Vec<Vec<usize>> = Vec::new();
    let mut group_centers: Vec<Bound<'py, PyAny>> = Vec::new();
    for index in super::tables::sorted_object_indices(py, &keys)? {
        let center = &seed_centers[index];
        let mut target = None;
        for (position, current) in group_centers.iter().enumerate() {
            if le
                .call1((
                    absolute.call1((sub.call1((center, current))?,))?,
                    &tolerance,
                ))?
                .is_truthy()?
            {
                target = Some(position);
                break;
            }
        }
        if let Some(position) = target {
            groups[position].extend(&seeds[index]);
            group_centers[position] = mean.call1((PyList::new(
                py,
                groups[position].iter().map(|&i| centers[i].clone()),
            )?,))?;
        } else {
            groups.push(seeds[index].clone());
            group_centers.push(center.clone());
        }
    }
    let mut rows = Vec::new();
    let row_keys = PyList::empty(py);
    for group in groups {
        let lefts = PyList::new(
            py,
            group
                .iter()
                .map(|&i| boxes[i].get_item(0))
                .collect::<PyResult<Vec<_>>>()?,
        )?;
        let group: Vec<_> = super::tables::sorted_object_indices(py, &lefts)?
            .into_iter()
            .map(|position| group[position])
            .collect();
        let members = PyList::new(py, group.iter().map(|&i| references[i].clone()))?;
        let center = divide.call1((
            total.call1((PyList::new(py, group.iter().map(|&i| centers[i].clone()))?,))?,
            group.len(),
        ))?;
        let bbox = if group.len() == 1 {
            boxes[group[0]].clone()
        } else {
            let mut coordinates = Vec::new();
            for axis in 0..4 {
                let values = PyList::new(
                    py,
                    group
                        .iter()
                        .map(|&i| boxes[i].get_item(axis))
                        .collect::<PyResult<Vec<_>>>()?,
                )?;
                coordinates.push((if axis < 2 { &minimum } else { &maximum }).call1((values,))?);
            }
            PyTuple::new(py, coordinates)?.into_any()
        };
        let ids = PySet::empty(py)?;
        for &i in &group {
            if !visual[i].is_none() {
                ids.add(&visual[i])?;
            }
        }
        let id = if ids.len() == 1 {
            ids.iter().next().unwrap().into_any()
        } else {
            py.None().into_bound(py)
        };
        row_keys.append(&center)?;
        rows.push(row_type.call1((members, center, bbox, id))?);
    }
    PyList::new(
        py,
        super::tables::sorted_object_indices(py, &row_keys)?
            .into_iter()
            .map(|index| rows[index].clone()),
    )
}
