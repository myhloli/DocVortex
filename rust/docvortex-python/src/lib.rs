//! 将 Python 批量参数转换为自有记录，再交给纯 Rust 核心计算。

use docvortex_core::dedup;
use docvortex_core::geometry::{self, Box4, Size};
use docvortex_core::tables;
use pyo3::prelude::*;
use pyo3::types::PyList;

/// 表格区域一次筛选整页字符，特殊输入继续沿 Python 的原验证入口处理。
#[pyfunction]
fn table_boxes(
    py: Python<'_>,
    values: &Bound<'_, PyList>,
    table: Box4,
    angle: i32,
    fallback: &Bound<'_, PyAny>,
) -> PyResult<Option<Vec<(usize, BoxTuple, Option<BoxTuple>)>>> {
    if table.iter().any(|v| !v.is_finite()) {
        return Ok(None);
    }
    let values = read_boxes(values, fallback)?;
    Ok(Some(py.detach(move || {
        tables::select_boxes(values, table, angle)
            .into_iter()
            .map(|(i, b, l)| (i, box_tuple(b), l.map(box_tuple)))
            .collect()
    })))
}

/// 对整批轨道查询计算覆盖率，避免逐线段绑定调用。
#[pyfunction]
fn coverage_batch(
    py: Python<'_>,
    rules: Vec<tables::Rule>,
    queries: Vec<tables::Query>,
) -> Option<Vec<f64>> {
    py.detach(move || tables::coverage_batch(rules, queries))
}

/// 按 Python 簇坐标批量合并相邻线段。
#[pyfunction]
fn merge_rules(
    py: Python<'_>,
    rules: Vec<tables::Rule>,
    coordinates: Vec<(u8, Vec<f64>)>,
    tolerance: f64,
    join: f64,
) -> Option<Vec<tables::Rule>> {
    py.detach(move || tables::merge_rules(rules, coordinates, tolerance, join))
}

/// 批量完成所有字符的单元格分配，保留索引和歧义位。
#[pyfunction]
fn assign_cells(
    py: Python<'_>,
    glyphs: Vec<Box4>,
    specs: Vec<Box4>,
    index: Option<tables::GridIndex>,
) -> Option<Vec<(Option<usize>, bool)>> {
    py.detach(move || tables::assign_cells(glyphs, specs, index))
}

/// 验证原子格索引后一次执行网格连接。
#[pyfunction]
fn grid_parents(py: Python<'_>, count: usize, pairs: Vec<(usize, usize)>) -> PyResult<Vec<usize>> {
    if pairs.iter().any(|(a, b)| *a >= count || *b >= count) {
        return Err(pyo3::exceptions::PyIndexError::new_err(
            "cell index out of range",
        ));
    }
    Ok(py.detach(move || tables::grid_parents(count, pairs)))
}

/// 将合法并查集转为矩形单元格，同时返回路径压缩后的父节点。
#[pyfunction]
fn component_specs(
    py: Python<'_>,
    parents: Vec<usize>,
    rows: usize,
    cols: usize,
) -> PyResult<(Vec<usize>, Option<Vec<(usize, usize, usize, usize)>>)> {
    if rows.checked_mul(cols) != Some(parents.len()) || parents.iter().any(|i| *i >= parents.len())
    {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "invalid table parents",
        ));
    }
    // 来源是私有 Python 并查集；在绑定边界阻止异常环导致原生死循环。
    for start in 0..parents.len() {
        let mut i = start;
        let mut steps = 0;
        while parents[i] != i {
            i = parents[i];
            steps += 1;
            if steps >= parents.len() {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "cyclic table parents",
                ));
            }
        }
    }
    Ok(py.detach(move || tables::component_specs(parents, rows, cols)))
}

/// 批量生成重复绘制候选，不在内层循环回调 Python。
#[pyfunction]
fn paint_pairs(
    py: Python<'_>,
    records: Vec<dedup::PaintRecord>,
) -> Option<(Vec<dedup::Pair>, Vec<dedup::Offset>)> {
    py.detach(move || dedup::paint_pairs(records))
}

/// 检查来源索引后批量计算最早来源连通关系。
#[pyfunction]
fn dedup_components(py: Python<'_>, count: usize, pairs: Vec<dedup::Pair>) -> PyResult<Vec<usize>> {
    if pairs.iter().any(|(a, b)| *a >= count || *b >= count) {
        return Err(pyo3::exceptions::PyIndexError::new_err(
            "glyph index out of range",
        ));
    }
    Ok(py.detach(move || dedup::components(count, &pairs)))
}

/// 返回隐藏 OCR 的几何候选，文本比较与来源物化仍由 Python 拥有。
#[pyfunction]
fn hidden_candidates(
    py: Python<'_>,
    records: Vec<dedup::HiddenRecord>,
) -> Option<Vec<(Vec<usize>, Vec<usize>)>> {
    py.detach(move || dedup::hidden_candidates(records))
}

/// 在原始候选顺序下确认平移证据，并验证全部来源索引。
#[pyfunction]
fn confirmed_offsets(
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

type BoxTuple = (f64, f64, f64, f64);
/// 在 Python 绑定层将框数组物化为原有不可变四元组。
fn box_tuple(b: Box4) -> BoxTuple {
    (b[0], b[1], b[2], b[3])
}

/// 普通数值直接提取；特殊可迭代输入仍使用原 Python 校验语义。
fn read_boxes(
    values: &Bound<'_, PyList>,
    fallback: &Bound<'_, PyAny>,
) -> PyResult<Vec<Option<Box4>>> {
    values
        .iter()
        .map(|v| match v.extract::<Option<Box4>>() {
            Ok(b) => Ok(b),
            Err(_) => fallback.call1((v,))?.extract::<Option<Box4>>(),
        })
        .collect()
}

/// 批量替代逐字符框转换，只在边界持有 Python 对象。
#[pyfunction]
fn normalize_boxes(
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
fn visual_runs(
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
fn local_boxes(
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

/// 准备 canonical 样本与风险筛选共享的批量几何，返回独立数值。
#[pyfunction]
fn source_rows(
    py: Python<'_>,
    raw: &Bound<'_, PyList>,
    side: &Bound<'_, PyList>,
    tight: &Bound<'_, PyList>,
    origins: Vec<Option<Size>>,
    rotations: Vec<f64>,
    size: Size,
    angle: i32,
    fallback: &Bound<'_, PyAny>,
) -> PyResult<Vec<Option<(BoxTuple, BoxTuple, Size, BoxTuple, BoxTuple, Size)>>> {
    let raw = read_boxes(raw, fallback)?;
    let side = read_boxes(side, fallback)?;
    let tight = read_boxes(tight, fallback)?;
    if [side.len(), tight.len(), origins.len(), rotations.len()]
        .iter()
        .any(|n| *n != raw.len())
    {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "geometry batch lengths differ",
        ));
    }
    let rows = raw
        .into_iter()
        .zip(side)
        .zip(tight)
        .zip(origins)
        .zip(rotations)
        .map(|((((r, s), t), o), a)| (r, s, t, o, a))
        .collect();
    Ok(py.detach(move || {
        geometry::source_rows(rows, size, angle)
            .into_iter()
            .map(|v| {
                v.map(|(s, t, o, ls, lt, lo)| {
                    (
                        box_tuple(s),
                        box_tuple(t),
                        o,
                        box_tuple(ls),
                        box_tuple(lt),
                        lo,
                    )
                })
            })
            .collect()
    }))
}

/// 一次提取已物化的数值特征，释放 GIL 后计算整段字符角色。
#[pyfunction]
fn script_roles(py: Python<'_>, records: Vec<docvortex_core::scripts::Record>) -> Option<Vec<u8>> {
    py.detach(move || docvortex_core::scripts::classify(records))
}

/// 注册私有扩展及协议号；公开 Python 接口仍由原模块提供。
#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("PROTOCOL_VERSION", docvortex_core::PROTOCOL_VERSION)?;
    module.add_function(wrap_pyfunction!(script_roles, module)?)?;
    module.add_function(wrap_pyfunction!(normalize_boxes, module)?)?;
    module.add_function(wrap_pyfunction!(visual_runs, module)?)?;
    module.add_function(wrap_pyfunction!(local_boxes, module)?)?;
    module.add_function(wrap_pyfunction!(source_rows, module)?)?;
    module.add_function(wrap_pyfunction!(paint_pairs, module)?)?;
    module.add_function(wrap_pyfunction!(dedup_components, module)?)?;
    module.add_function(wrap_pyfunction!(hidden_candidates, module)?)?;
    module.add_function(wrap_pyfunction!(confirmed_offsets, module)?)?;
    module.add_function(wrap_pyfunction!(table_boxes, module)?)?;
    module.add_function(wrap_pyfunction!(coverage_batch, module)?)?;
    module.add_function(wrap_pyfunction!(merge_rules, module)?)?;
    module.add_function(wrap_pyfunction!(assign_cells, module)?)?;
    module.add_function(wrap_pyfunction!(grid_parents, module)?)?;
    module.add_function(wrap_pyfunction!(component_specs, module)?)?;
    Ok(())
}
