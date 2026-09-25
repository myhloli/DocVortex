//! 有序聚类、字体与栏间距的批量统计绑定。

use docvortex_core::geometry::Box4;
use pyo3::prelude::*;

/// 批量聚类仅返回索引，避免为簇成员复制坐标与公开对象。
#[pyfunction]
pub(super) fn ordered_clusters(
    py: Python<'_>,
    values: Vec<f64>,
    tolerance: f64,
    relative: f64,
    last_only: bool,
) -> Option<Vec<Vec<usize>>> {
    py.detach(move || {
        docvortex_core::statistics::ordered_clusters(values, tolerance, relative, last_only)
    })
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
        docvortex_core::statistics::typography(boxes, fonts, weights, families, fallback_height)
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
