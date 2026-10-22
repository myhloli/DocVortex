//! 同一次候选构建持有填充框快照；完整数值筛选在 Rust 内执行，不保留 Python 参考算法。
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyFloat;

/// 普通有限浮点使用释放 GIL 的快路径；其他数值在绑定层保留解释器算术语义。
fn number(value: &Bound<'_, PyAny>) -> Option<f64> {
    if !value.is_exact_instance_of::<PyFloat>() {
        return None;
    }
    let value = value.extract::<f64>().ok()?;
    (value.is_finite() && value.abs() <= 1e100).then_some(value)
}

/// 只读打包已知浮点坐标，整数等原对象仍由快照持有以保留精度。
fn plain_bbox(py: Python<'_>, values: &[Py<PyAny>; 4]) -> Option<[f64; 4]> {
    let mut result = [0.0; 4];
    for (target, value) in result.iter_mut().zip(values) {
        *target = number(value.bind(py))?;
    }
    Some(result)
}

/// 原生算法持有原数值和可用的浮点快照，不保存任何最终候选或跨页面状态。
#[pyclass]
pub(super) struct NativeFillBands {
    raw: Vec<[Py<PyAny>; 4]>,
    plain: Option<Vec<[f64; 4]>>,
}

#[pymethods]
impl NativeFillBands {
    /// 完整记录输入坐标，错误形状立即失败，合法整数和特殊浮点不再请求参考后端。
    #[new]
    fn new(py: Python<'_>, boxes: Vec<Vec<Py<PyAny>>>) -> PyResult<Self> {
        let raw: Vec<[Py<PyAny>; 4]> = boxes
            .into_iter()
            .map(|box_| {
                box_.try_into()
                    .map_err(|_| PyValueError::new_err("Expected four fill-band coordinates"))
            })
            .collect::<PyResult<_>>()?;
        let plain = raw.iter().map(|box_| plain_bbox(py, box_)).collect();
        Ok(Self { raw, plain })
    }

    /// 每个查询保留边界、重叠去重和首个分组语义，特殊数值仍由 Rust 控制完整循环。
    fn count(
        &self,
        py: Python<'_>,
        rule: Vec<Py<PyAny>>,
        em: &Bound<'_, PyAny>,
    ) -> PyResult<usize> {
        let rule: [Py<PyAny>; 4] = rule
            .try_into()
            .map_err(|_| PyValueError::new_err("Expected four rule coordinates"))?;
        if let (Some(boxes), Some(bounds), Some(height)) =
            (&self.plain, plain_bbox(py, &rule), number(em))
        {
            return Ok(py.detach(|| {
                docvortex_core::table_candidates::repeated_fill_bands(boxes, bounds, height)
            }));
        }
        count_generic(py, &self.raw, &rule, em)
    }
}

/// 使用原始数值对象执行同一筛选和聚类，保持大整数、NaN 和有符号零的 Python 运算次序。
fn count_generic(
    py: Python<'_>,
    boxes: &[[Py<PyAny>; 4]],
    rule: &[Py<PyAny>; 4],
    em: &Bound<'_, PyAny>,
) -> PyResult<usize> {
    let builtins = py.import("builtins")?;
    let operators = py.import("operator")?;
    let minimum = builtins.getattr("min")?;
    let maximum = builtins.getattr("max")?;
    let absolute = builtins.getattr("abs")?;
    let sub = operators.getattr("sub")?;
    let add = operators.getattr("add")?;
    let mul = operators.getattr("mul")?;
    let div = operators.getattr("truediv")?;
    let lt = operators.getattr("lt")?;
    let le = operators.getattr("le")?;
    let gt = operators.getattr("gt")?;
    let ge = operators.getattr("ge")?;
    let axis_overlap =
        |a: &[Py<PyAny>; 4], b: &[Py<PyAny>; 4], axis: usize| -> PyResult<Bound<'_, PyAny>> {
            maximum.call1((
                0.0,
                sub.call1((
                    minimum.call1((a[axis + 2].bind(py), b[axis + 2].bind(py)))?,
                    maximum.call1((a[axis].bind(py), b[axis].bind(py)))?,
                ))?,
            ))
        };
    let area = |box_: &[Py<PyAny>; 4]| -> PyResult<Bound<'_, PyAny>> {
        mul.call1((
            maximum.call1((0.0, sub.call1((box_[2].bind(py), box_[0].bind(py)))?))?,
            maximum.call1((0.0, sub.call1((box_[3].bind(py), box_[1].bind(py)))?))?,
        ))
    };
    let minimum_width = maximum.call1((
        mul.call1((8.0, em))?,
        mul.call1((0.3, sub.call1((rule[2].bind(py), rule[0].bind(py)))?))?,
    ))?;
    let mut candidates: Vec<usize> = Vec::new();
    for (index, box_) in boxes.iter().enumerate() {
        let width = sub.call1((box_[2].bind(py), box_[0].bind(py)))?;
        let height = sub.call1((box_[3].bind(py), box_[1].bind(py)))?;
        if lt.call1((&width, &minimum_width))?.is_truthy()?
            || !le.call1((mul.call1((0.25, em))?, &height))?.is_truthy()?
            || !le.call1((&height, mul.call1((3.0, em))?))?.is_truthy()?
        {
            continue;
        }
        let center = div.call1((add.call1((box_[1].bind(py), box_[3].bind(py)))?, 2.0))?;
        if lt.call1((&center, rule[1].bind(py)))?.is_truthy()?
            || gt.call1((&center, rule[3].bind(py)))?.is_truthy()?
        {
            continue;
        }
        let shorter = minimum.call1((&width, sub.call1((rule[2].bind(py), rule[0].bind(py)))?))?;
        let ratio = if gt.call1((&shorter, 0))?.is_truthy()? {
            div.call1((axis_overlap(box_, rule, 0)?, shorter))?
        } else {
            0.0_f64.into_pyobject(py)?.into_any()
        };
        if lt.call1((ratio, 0.8))?.is_truthy()? {
            continue;
        }
        let mut duplicate = false;
        for &prior in &candidates {
            let other = &boxes[prior];
            let smaller = minimum.call1((area(box_)?, area(other)?))?;
            if gt.call1((&smaller, 0))?.is_truthy()? {
                let ratio = div.call1((
                    mul.call1((axis_overlap(box_, other, 0)?, axis_overlap(box_, other, 1)?))?,
                    smaller,
                ))?;
                if ge.call1((ratio, 0.9))?.is_truthy()? {
                    duplicate = true;
                    break;
                }
            }
        }
        if !duplicate {
            candidates.push(index);
        }
    }
    let tolerance = maximum.call1((3.0, em))?;
    let mut groups: Vec<(usize, usize)> = Vec::new();
    for index in candidates {
        let box_ = &boxes[index];
        let mut matched = false;
        for (first, count) in &mut groups {
            let initial = &boxes[*first];
            if le
                .call1((
                    absolute.call1((sub.call1((box_[0].bind(py), initial[0].bind(py)))?,))?,
                    &tolerance,
                ))?
                .is_truthy()?
                && le
                    .call1((
                        absolute.call1((sub.call1((box_[2].bind(py), initial[2].bind(py)))?,))?,
                        &tolerance,
                    ))?
                    .is_truthy()?
                && le
                    .call1((
                        absolute.call1((sub.call1((
                            sub.call1((box_[3].bind(py), box_[1].bind(py)))?,
                            sub.call1((initial[3].bind(py), initial[1].bind(py)))?,
                        ))?,))?,
                        &tolerance,
                    ))?
                    .is_truthy()?
            {
                *count += 1;
                matched = true;
                break;
            }
        }
        if !matched {
            groups.push((index, 1));
        }
    }
    Ok(groups.iter().map(|(_, count)| *count).max().unwrap_or(0))
}

/// 在当前构建边界准备完整原生快照，任何合法数值都不再退回 Python 计数。
#[pyfunction]
pub(super) fn prepare_fill_bands(
    py: Python<'_>,
    boxes: Vec<Vec<Py<PyAny>>>,
) -> PyResult<NativeFillBands> {
    NativeFillBands::new(py, boxes)
}
