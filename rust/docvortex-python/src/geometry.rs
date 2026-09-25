//! 字符几何与坐标转换绑定，保留 Python 坐标对象复用语义。

use docvortex_core::extraction;
use docvortex_core::geometry::{self, Box4, Size};
use pyo3::prelude::*;
use pyo3::types::{PyFloat, PyList, PyTuple};

use super::conversion::{box_tuple, read_boxes, BoxTuple};

/// 在 PDFium 读取结束后批量转换数值，不接触句柄或同步锁。
#[pyfunction]
pub(super) fn materialize_geometry(
    py: Python<'_>,
    rows: Vec<extraction::RawGeometry>,
    frame: Box4,
    rounded: Size,
    angle: i32,
) -> Vec<(Box4, Option<BoxTuple>, Option<BoxTuple>, Option<(f64, f64)>)> {
    py.detach(move || {
        extraction::materialize(rows, frame, rounded, angle)
            .into_iter()
            .map(|(layout, loose, tight, origin)| {
                (
                    layout,
                    loose.map(box_tuple),
                    tight.map(box_tuple),
                    origin.map(|p| (p[0], p[1])),
                )
            })
            .collect()
    })
}

/// 一次消费整行锚点，不回传重复坐标，只返回合格相邻对的统计量。
#[pyfunction]
pub(super) fn anchor_pairs(
    py: Python<'_>,
    records: Vec<(usize, Box4, Box4, Size, f64)>,
    positive_source: bool,
) -> Option<Vec<(usize, f64, f64, bool)>> {
    py.detach(move || geometry::anchor_pairs(records, positive_source))
}

/// 批量替代逐字符框转换，只在边界持有 Python 对象。
#[pyfunction]
pub(super) fn normalize_boxes(
    py: Python<'_>,
    values: &Bound<'_, PyList>,
    strict: bool,
    fallback: &Bound<'_, PyAny>,
) -> PyResult<Vec<Option<BoxTuple>>> {
    let values = read_boxes(values, fallback)?;
    Ok(py.detach(move || {
        values
            .into_iter()
            .map(|b| geometry::normalize(b, strict).map(box_tuple))
            .collect()
    }))
}

/// 一次完成整行裁剪、旋转、字距统计与分组；结果保留源范围。
#[pyfunction]
pub(super) fn visual_runs(
    py: Python<'_>,
    raw: &Bound<'_, PyList>,
    overrides: &Bound<'_, PyList>,
    flags: Vec<u8>,
    size: Size,
    angle: i32,
    fallback: &Bound<'_, PyAny>,
) -> PyResult<Vec<(usize, usize, Option<BoxTuple>)>> {
    let raw = read_boxes(raw, fallback)?;
    let overrides = read_boxes(overrides, fallback)?;
    if raw.len() != overrides.len() || raw.len() != flags.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "geometry batch lengths differ",
        ));
    }
    Ok(py.detach(move || {
        geometry::visual_runs(raw, overrides, flags, size, angle)
            .into_iter()
            .map(|(a, b, c)| (a, b, c.map(box_tuple)))
            .collect()
    }))
}

/// 一次计算字体统计所需的合法局部框，保持空字符对应的原索引。
#[pyfunction]
pub(super) fn local_boxes(
    py: Python<'_>,
    values: &Bound<'_, PyList>,
    size: Size,
    angle: i32,
    fallback: &Bound<'_, PyAny>,
) -> PyResult<Vec<Option<BoxTuple>>> {
    let values = read_boxes(values, fallback)?;
    Ok(py.detach(move || {
        values
            .into_iter()
            .map(|b| {
                geometry::clip(geometry::normalize(b, false), size)
                    .map(|b| box_tuple(geometry::rotate(b, size, angle)))
            })
            .collect()
    }))
}

/// 为未改变的坐标复用已有 Python 浮点对象，避免 canonical 样本复制整本坐标。
fn shared_coordinates<'py, const N: usize>(
    py: Python<'py>,
    values: [f64; N],
    candidates: &[Bound<'py, PyAny>],
) -> PyResult<Bound<'py, PyAny>> {
    let mut output = Vec::with_capacity(N);
    for (index, value) in values.into_iter().enumerate() {
        let mut shared = None;
        for candidate in candidates {
            // 只读取普通传输容器；特殊输入已由原校验器处理，不再次触发用户方法。
            if !candidate.is_exact_instance_of::<PyList>()
                && !candidate.is_exact_instance_of::<PyTuple>()
            {
                continue;
            }
            if let Ok(item) = candidate.get_item(index) {
                if item.is_exact_instance_of::<PyFloat>()
                    && item.extract::<f64>()?.to_bits() == value.to_bits()
                {
                    shared = Some(item);
                    break;
                }
            }
        }
        output.push(shared.unwrap_or_else(|| PyFloat::new(py, value).into_any()));
    }
    Ok(PyTuple::new(py, output)?.into_any())
}

/// 数值仍批量计算，但未旋转的局部框复用 source/tight tuple，不复制长期存活的坐标。
#[pyfunction]
pub(super) fn source_rows<'py>(
    py: Python<'py>,
    raw: &Bound<'py, PyList>,
    side: &Bound<'py, PyList>,
    tight: &Bound<'py, PyList>,
    origins: &Bound<'py, PyList>,
    rotations: Vec<f64>,
    size: Size,
    angle: i32,
    fallback: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyList>> {
    let raw_values = read_boxes(raw, fallback)?;
    let side_values = read_boxes(side, fallback)?;
    let tight_values = read_boxes(tight, fallback)?;
    let origin_values = origins.extract::<Vec<Option<Size>>>()?;
    if [
        side_values.len(),
        tight_values.len(),
        origin_values.len(),
        rotations.len(),
    ]
    .iter()
    .any(|n| *n != raw_values.len())
    {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "geometry batch lengths differ",
        ));
    }
    let rows = raw_values
        .into_iter()
        .zip(side_values)
        .zip(tight_values)
        .zip(origin_values)
        .zip(rotations)
        .map(|((((r, s), t), o), a)| (r, s, t, o, a))
        .collect();
    let prepared = py.detach(move || geometry::source_rows(rows, size, angle));
    let output = PyList::empty(py);
    for (index, row) in prepared.into_iter().enumerate() {
        if let Some((s, t, o, ls, lt, lo)) = row {
            let source = shared_coordinates(py, s, &[raw.get_item(index)?, side.get_item(index)?])?;
            let tight_box = shared_coordinates(py, t, &[tight.get_item(index)?])?;
            let origin = shared_coordinates(py, o, &[origins.get_item(index)?])?;
            let local_source = if angle == 0 {
                source.clone()
            } else {
                shared_coordinates(py, ls, &[])?
            };
            let local_tight = if angle == 0 {
                tight_box.clone()
            } else {
                shared_coordinates(py, lt, &[])?
            };
            let local_origin = if angle == 0 {
                origin.clone()
            } else {
                shared_coordinates(py, lo, &[])?
            };
            output.append(PyTuple::new(
                py,
                [
                    source,
                    tight_box,
                    origin,
                    local_source,
                    local_tight,
                    local_origin,
                ],
            )?)?;
        } else {
            output.append(py.None())?;
        }
    }
    Ok(output)
}
