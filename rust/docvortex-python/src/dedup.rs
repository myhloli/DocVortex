//! 重复绘制、隐藏文字候选与映射分组的批量绑定。

use docvortex_core::dedup;
use pyo3::prelude::*;

/// 批量生成重复绘制候选，不在内层循环回调 Python。
#[pyfunction]
pub(super) fn paint_pairs(
    py: Python<'_>,
    records: Vec<dedup::PaintRecord>,
) -> Option<(Vec<dedup::Pair>, Vec<dedup::Offset>)> {
    py.detach(move || dedup::paint_pairs(records))
}

/// 检查来源索引后批量计算最早来源连通关系。
#[pyfunction]
pub(super) fn dedup_components(
    py: Python<'_>,
    count: usize,
    pairs: Vec<dedup::Pair>,
) -> PyResult<Vec<usize>> {
    if pairs.iter().any(|(a, b)| *a >= count || *b >= count) {
        return Err(pyo3::exceptions::PyIndexError::new_err(
            "glyph index out of range",
        ));
    }
    Ok(py.detach(move || dedup::components(count, &pairs)))
}

/// 返回隐藏 OCR 的几何候选，文本比较与来源物化仍由 Python 拥有。
#[pyfunction]
pub(super) fn hidden_candidates(
    py: Python<'_>,
    records: Vec<dedup::HiddenRecord>,
) -> Option<Vec<(Vec<usize>, Vec<usize>)>> {
    py.detach(move || dedup::hidden_candidates(records))
}

/// 在原始候选顺序下确认平移证据，并验证全部来源索引。
#[pyfunction]
pub(super) fn confirmed_offsets(
    py: Python<'_>,
    records: Vec<dedup::EvidenceRecord>,
    pairs: Vec<dedup::Offset>,
    exact: Vec<dedup::Pair>,
) -> PyResult<Vec<dedup::Pair>> {
    if pairs
        .iter()
        .any(|p| p.0 >= records.len() || p.1 >= records.len())
        || exact
            .iter()
            .any(|(a, b)| *a >= records.len() || *b >= records.len())
    {
        return Err(pyo3::exceptions::PyIndexError::new_err(
            "glyph index out of range",
        ));
    }
    Ok(py.detach(move || dedup::confirmed_offsets(records, pairs, exact)))
}

/// 只传递一次每字符数值，返回分组范围以复用原有字符对象。
#[pyfunction]
pub(super) fn mapping_runs(
    py: Python<'_>,
    records: Vec<dedup::MappingRecord>,
) -> Option<Vec<(usize, usize)>> {
    py.detach(move || dedup::mapping_runs(records))
}
