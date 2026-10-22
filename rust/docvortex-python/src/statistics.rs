//! 有序聚类、字体与栏间距的批量统计绑定。

use docvortex_core::geometry::Box4;
use pyo3::prelude::*;

/// 普通浮点均值由 Rust 精确累积；其他数值类型保留宿主标准库的精确除法与异常契约。
#[pyfunction]
pub(super) fn exact_float_mean<'py>(
    py: Python<'py>,
    values: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    use pyo3::types::{PyFloat, PyList};
    if values.is_exact_instance_of::<PyList>() {
        let mut numbers = Vec::new();
        let mut ordinary = true;
        for value in values.try_iter()? {
            let value = value?;
            if !value.is_exact_instance_of::<PyFloat>() {
                ordinary = false;
                break;
            }
            numbers.push(value.extract::<f64>()?);
        }
        if ordinary {
            if let Some((total, power)) = docvortex_core::exact_mean::exact_sum_parts(&numbers) {
                let mut numerator = total.into_pyobject(py)?.into_any();
                let mut denominator = numbers.len().into_pyobject(py)?.into_any();
                if power >= 0 {
                    numerator = numerator.call_method1("__lshift__", (power,))?;
                } else {
                    denominator = denominator.call_method1("__lshift__", (-power,))?;
                }
                // 持有 GIL 与两个普通整数，复用 CPython 精确整数真除法的最终舍入。
                return unsafe {
                    Bound::<PyAny>::from_owned_ptr_or_err(
                        py,
                        pyo3::ffi::PyNumber_TrueDivide(numerator.as_ptr(), denominator.as_ptr()),
                    )
                };
            }
        }
    }
    py.import("statistics")?.getattr("mean")?.call1((values,))
}

/// 批量聚类仅返回索引，避免为簇成员复制坐标与公开对象。
#[pyfunction]
pub(super) fn ordered_clusters(
    py: Python<'_>,
    values: Vec<f64>,
    tolerance: f64,
    relative: f64,
    last_only: bool,
) -> PyResult<Option<Vec<Vec<usize>>>> {
    if values.iter().all(|v| v.is_finite()) && tolerance.is_finite() && relative.is_finite() {
        return Ok(py.detach(move || {
            docvortex_core::statistics::ordered_clusters(values, tolerance, relative, last_only)
        }));
    }
    // NaN 的比较不是全序；借用当前解释器的稳定排序保持原中位数及来源次序。
    use pyo3::types::{PyDict, PyList};
    let numbers = PyList::new(py, &values)?;
    let order = PyList::new(py, 0..values.len())?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("key", numbers.getattr("__getitem__")?)?;
    order.call_method("sort", (), Some(&kwargs))?;
    let mut clusters: Vec<Vec<usize>> = Vec::new();
    for index in order.extract::<Vec<usize>>()? {
        let start = if last_only {
            clusters.len().saturating_sub(1)
        } else {
            0
        };
        let mut target = None;
        for (candidate, cluster) in clusters.iter().enumerate().skip(start) {
            let sample = PyList::new(py, cluster.iter().map(|i| values[*i]))?;
            sample.call_method0("sort")?;
            let sorted = sample.extract::<Vec<f64>>()?;
            let n = sorted.len();
            let middle = if n % 2 == 1 {
                sorted[n / 2]
            } else {
                (sorted[n / 2 - 1] + sorted[n / 2]) / 2.0
            };
            let limit = if relative == 0.0 {
                tolerance
            } else {
                relative * middle
            };
            let delta = (values[index] - middle).abs();
            if if last_only {
                delta.partial_cmp(&limit) != Some(std::cmp::Ordering::Greater)
            } else {
                delta <= limit
            } {
                target = Some(candidate);
                break;
            }
        }
        if let Some(target) = target {
            clusters[target].push(index);
        } else {
            clusters.push(vec![index]);
        }
    }
    Ok(Some(clusters))
}

/// 聚合已验证的局部框，返回字号、主字体索引和行首特征，不返回重复字符记录。
#[pyfunction]
pub(super) fn typography_metrics(
    py: Python<'_>,
    boxes: Vec<Option<Box4>>,
    fonts: Vec<Option<usize>>,
    weights: Vec<Option<f64>>,
    families: Vec<Option<usize>>,
    fallback_height: f64,
) -> Option<docvortex_core::statistics::Typography> {
    py.detach(move || {
        docvortex_core::statistics::typography(boxes, fonts, weights, &families, fallback_height)
    })
}

/// 栏带调用内一次消费稳定快照，保留 Python 成员排序的副作用。
#[pyfunction]
pub(super) fn lane_gap(
    py: Python<'_>,
    boxes: Vec<Box4>,
    heights: Vec<f64>,
    restored: Vec<bool>,
    skip: Vec<bool>,
) -> Option<(f64, f64)> {
    py.detach(move || docvortex_core::statistics::lane_gap(boxes, heights, restored, skip))
}
