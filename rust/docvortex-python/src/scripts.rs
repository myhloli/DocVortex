//! 上下标分类与配对绑定，Python 继续负责文本解释与物化。

use docvortex_core::geometry::{self, Size};
use pyo3::prelude::*;
use pyo3::types::PyList;

use super::conversion::read_boxes;

/// 批量计算行内上下标双向最近配对，Python 继续负责递归物化。
#[pyfunction]
pub(super) fn inline_script_matches(
    py: Python<'_>,
    records: Vec<docvortex_core::inline_pairs::Record>,
) -> Option<Vec<(usize, usize, bool, bool)>> {
    py.detach(|| docvortex_core::inline_pairs::matches(records))
}

/// 一次提取已物化的数值特征，释放 GIL 后计算整段字符角色。
#[pyfunction]
pub(super) fn script_roles(
    py: Python<'_>,
    records: Vec<docvortex_core::scripts::Record>,
) -> Option<Vec<u8>> {
    py.detach(move || docvortex_core::scripts::classify(records))
}

/// 合并校验与分类边界，避免为每个字符创建两份中间 Python 坐标对象。
#[pyfunction]
pub(super) fn script_roles_raw(
    py: Python<'_>,
    loose: &Bound<'_, PyList>,
    tight: &Bound<'_, PyList>,
    origins: Vec<Option<Size>>,
    flags: Vec<u32>,
    fonts: Vec<i64>,
    fallback: &Bound<'_, PyAny>,
) -> PyResult<Option<Vec<u8>>> {
    let loose = read_boxes(loose, fallback)?;
    let tight = read_boxes(tight, fallback)?;
    if [tight.len(), origins.len(), flags.len(), fonts.len()]
        .iter()
        .any(|n| *n != loose.len())
    {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "script batch lengths differ",
        ));
    }
    Ok(py.detach(move || {
        let records = loose
            .into_iter()
            .zip(tight)
            .zip(origins)
            .zip(flags)
            .zip(fonts)
            .map(|((((l, t), o), mut flag), font)| {
                let l = geometry::normalize(l, true).unwrap_or([0.0; 4]);
                let t = geometry::normalize(t, true);
                let o = o.filter(|p| p.iter().all(|v| v.is_finite()));
                if flag & 3 == 0 && t.is_some() && o.is_some() {
                    flag |= 256;
                }
                (l, t, o, flag, font)
            })
            .collect();
        docvortex_core::scripts::classify(records)
    }))
}
