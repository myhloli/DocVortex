//! 表格计算与调用内索引状态的 Python 绑定。

use docvortex_core::geometry::Box4;
use docvortex_core::tables;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyList};

use super::conversion::{box_tuple, read_boxes, BoxTuple};

/// 行范围极值索引，不向 Python 返回重复浮点坐标。
#[pyclass(frozen)]
pub(super) struct TableRowGeometry {
    index: docvortex_core::row_geometry::RowGeometry,
}

#[pymethods]
impl TableRowGeometry {
    /// 任意行集合仍由原生层求并集，有限浮点使用核心索引，其余保留精确比较。
    #[staticmethod]
    fn union_generic(py: Python<'_>, boxes: Vec<Vec<Py<PyAny>>>) -> PyResult<Option<[usize; 4]>> {
        let mut ordinary = Vec::with_capacity(boxes.len());
        for bbox in &boxes {
            if bbox.len() != 4
                || bbox
                    .iter()
                    .any(|v| !v.bind(py).is_exact_instance_of::<pyo3::types::PyFloat>())
            {
                return super::table_candidates::union_source_indices(py, &boxes);
            }
            let values = [
                bbox[0].extract::<f64>(py)?,
                bbox[1].extract::<f64>(py)?,
                bbox[2].extract::<f64>(py)?,
                bbox[3].extract::<f64>(py)?,
            ];
            if values.iter().any(|value| !value.is_finite()) {
                return super::table_candidates::union_source_indices(py, &boxes);
            }
            ordinary.push(values);
        }
        let count = ordinary.len();
        Ok(py.detach(|| {
            docvortex_core::row_geometry::RowGeometry::new(ordinary)
                .and_then(|index| index.union_indices(0, count))
        }))
    }

    /// 构建时一次验证全部有限框，非法输入必须交回参考实现。
    #[new]
    fn new(py: Python<'_>, boxes: Vec<[f64; 4]>) -> PyResult<Self> {
        py.detach(|| docvortex_core::row_geometry::RowGeometry::new(boxes))
            .map(|index| Self { index })
            .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("nonfinite row geometry"))
    }

    /// 返回四个坐标分别来自哪一行，Python 复用已有坐标对象。
    fn union_indices(&self, py: Python<'_>, start: usize, end: usize) -> Option<[usize; 4]> {
        py.detach(|| self.index.union_indices(start, end))
    }

    /// 保留原行顺序，只跳过下边界一定不超过表底的前缀。
    fn first_after(&self, py: Python<'_>, bottom: f64) -> Option<usize> {
        if !bottom.is_finite() {
            return None;
        }
        Some(py.detach(|| self.index.first_after(bottom)))
    }
}

/// 调用内的稳定列累计状态，均值读取不折叠补偿量。
#[pyclass]
pub(super) struct StableColumnClusters {
    state: docvortex_core::columns::Columns,
}

#[pymethods]
impl StableColumnClusters {
    /// Python 边界依据解释器版本选择已验证的浮点求和模式。
    #[new]
    fn new(compensated: bool) -> Self {
        Self {
            state: docvortex_core::columns::Columns::new(compensated),
        }
    }

    /// 特殊数值仍在原生侧完成首命中聚类，算术委托解释器内建操作以保留整数和当前版本求和语义。
    #[staticmethod]
    fn count_generic(
        py: Python<'_>,
        rows: Vec<Vec<(Py<PyAny>, Py<PyAny>)>>,
        tolerance: &Bound<'_, PyAny>,
    ) -> PyResult<(usize, f64)> {
        use std::collections::HashSet;
        let operators = py.import("operator")?;
        let builtins = py.import("builtins")?;
        let add = operators.getattr("add")?;
        let subtract = operators.getattr("sub")?;
        let divide = operators.getattr("truediv")?;
        let absolute = operators.getattr("abs")?;
        let less_equal = operators.getattr("le")?;
        let as_float = builtins.getattr("float")?;
        let sum = builtins.getattr("sum")?;
        let mut best = (0, 0.0);
        if rows.is_empty() {
            return Ok(best);
        }
        for alignment in 0..3 {
            let mut clusters: Vec<(Py<PyAny>, Vec<Py<PyAny>>, HashSet<usize>)> = Vec::new();
            for (row_index, row) in rows.iter().enumerate() {
                for (left, right) in row {
                    let anchor = match alignment {
                        0 => left.bind(py).clone(),
                        1 => divide.call1((add.call1((left.bind(py), right.bind(py)))?, 2))?,
                        _ => right.bind(py).clone(),
                    };
                    let mut matched = None;
                    for (index, (mean, _, _)) in clusters.iter().enumerate() {
                        let distance = absolute.call1((
                            subtract.call1((&anchor, as_float.call1((mean.bind(py),))?))?,
                        ))?;
                        if less_equal.call1((distance, tolerance))?.is_truthy()? {
                            matched = Some(index);
                            break;
                        }
                    }
                    if let Some(index) = matched {
                        let (mean, values, members) = &mut clusters[index];
                        values.push(anchor.unbind());
                        members.insert(row_index);
                        let total =
                            sum.call1((PyList::new(py, values.iter().map(|v| v.bind(py)))?,))?;
                        *mean = divide.call1((total, values.len()))?.unbind();
                    } else {
                        clusters.push((
                            anchor.clone().unbind(),
                            vec![anchor.unbind()],
                            HashSet::from([row_index]),
                        ));
                    }
                }
            }
            let coverages: Vec<_> = clusters
                .iter()
                .map(|(_, _, members)| members.len() as f64 / rows.len() as f64)
                .filter(|coverage| *coverage >= 0.5)
                .collect();
            let result = (
                coverages.len(),
                coverages.iter().copied().reduce(f64::min).unwrap_or(0.0),
            );
            if result > best {
                best = result;
            }
        }
        Ok(best)
    }

    /// 只传入严格前缀之后的新增行，纯数值计算期间释放 GIL。
    fn extend(
        &mut self,
        py: Python<'_>,
        rows: Vec<Vec<(f64, f64)>>,
        tolerance: f64,
    ) -> Option<(usize, f64)> {
        py.detach(|| self.state.extend(&rows, tolerance))
    }

    /// 为差分测试返回均值、累计结果、成员数和覆盖行数，不暴露 Python 对象。
    fn snapshot(&self) -> Vec<Vec<(f64, f64, usize, usize)>> {
        self.state
            .groups
            .iter()
            .map(|group| {
                group
                    .iter()
                    .map(|c| {
                        let sum = if c.lo != 0.0 && c.lo.is_finite() {
                            c.hi + c.lo
                        } else {
                            c.hi
                        };
                        (c.mean, sum, c.count, c.rows)
                    })
                    .collect()
            })
            .collect()
    }
}

/// 为整页候选复用排序后的正文高度，查询仅传入区间及核心来源。
#[pyclass(frozen)]
pub(super) struct TableNoteMetrics {
    metrics: std::sync::Arc<docvortex_core::statistics::NoteMetrics>,
}

/// 已验证走廊行的来源成员及其中心范围，只在当前候选组复用。
#[pyclass(frozen)]
pub(super) struct TableNoteCore {
    rows: docvortex_core::note_index::CoreRows,
    owner: std::sync::Arc<docvortex_core::statistics::NoteMetrics>,
}

#[pymethods]
impl TableNoteMetrics {
    /// 非有限高度、任意精度来源编号及未准备样本仍由原生侧筛选，保留 bisect 和 median 的宿主语义。
    #[staticmethod]
    fn height_generic<'py>(
        py: Python<'py>,
        items: Vec<(Py<PyAny>, Py<PyAny>, Py<PyAny>)>,
        top: &Bound<'py, PyAny>,
        bottom: &Bound<'py, PyAny>,
        core: &Bound<'py, PyAny>,
        fallback: &Bound<'py, PyAny>,
        prepared: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let operators = py.import("operator")?;
        let le = operators.getattr("le")?;
        let core = py.import("builtins")?.getattr("set")?.call1((core,))?;
        let selected: Vec<usize> = if prepared {
            let centers = PyList::new(py, items.iter().map(|(_, center, _)| center.bind(py)))?;
            let bisect = py.import("bisect")?;
            let left = bisect
                .getattr("bisect_left")?
                .call1((&centers, top))?
                .extract::<usize>()?;
            let right = bisect
                .getattr("bisect_right")?
                .call1((&centers, bottom))?
                .extract::<usize>()?;
            (0..left).chain(right..items.len()).collect()
        } else {
            let mut selected = Vec::new();
            for (index, (_, center, _)) in items.iter().enumerate() {
                if !(le.call1((top, center.bind(py)))?.is_truthy()?
                    && le.call1((center.bind(py), bottom))?.is_truthy()?)
                {
                    selected.push(index);
                }
            }
            selected
        };
        let heights = PyList::empty(py);
        for index in selected {
            let (source, _, height) = &items[index];
            if !core.contains(source.bind(py))? {
                heights.append(height.bind(py))?;
            }
        }
        if heights.len() < 4 {
            return Ok(fallback.clone());
        }
        heights.call_method0("sort")?;
        let count = heights.len().div_ceil(4);
        py.import("statistics")?
            .getattr("median")?
            .call1((heights.get_slice(heights.len() - count, heights.len()),))
    }
    /// 一次性接收全部走廊行成员，后续候选仅传入行区间。
    fn prepare_rows(&self, py: Python<'_>, members: Vec<Vec<i64>>) -> TableNoteCore {
        TableNoteCore {
            rows: py.detach(|| self.metrics.rows(members)),
            owner: self.metrics.clone(),
        }
    }

    /// 跳过 Python 成员校验与打包，索引不适用时仍由 Rust 精确排除来源。
    fn height_for_rows(
        &self,
        py: Python<'_>,
        rows: &TableNoteCore,
        start: usize,
        end: usize,
        top: f64,
        bottom: f64,
        fallback: f64,
    ) -> Option<f64> {
        if !std::sync::Arc::ptr_eq(&self.metrics, &rows.owner) {
            return None;
        }
        py.detach(|| {
            self.metrics
                .height_for_rows(&rows.rows, start, end, top, bottom, fallback)
        })
    }
    /// 拒绝非有限数值，确保参考实现负责特殊排序语义。
    #[new]
    fn new(py: Python<'_>, items: Vec<(i64, f64, f64)>) -> PyResult<Self> {
        py.detach(move || docvortex_core::statistics::NoteMetrics::new(items))
            .map(|metrics| Self {
                metrics: std::sync::Arc::new(metrics),
            })
            .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("invalid note metrics"))
    }

    /// 数值筛选时释放 GIL，有限输入溢出返回参考路径标志。
    fn height(
        &self,
        py: Python<'_>,
        top: f64,
        bottom: f64,
        core: Vec<i64>,
        fallback: f64,
    ) -> Option<f64> {
        py.detach(|| self.metrics.height(top, bottom, &core, fallback))
    }
}

/// 表格组行只返回成员索引，由 Python 复用原字形对象。
#[pyfunction]
pub(super) fn table_visual_rows(
    py: Python<'_>,
    boxes: &Bound<'_, PyAny>,
    ids: &Bound<'_, PyAny>,
    median_height: f64,
) -> PyResult<Vec<Vec<usize>>> {
    let objects = object_boxes(boxes)?;
    let ids = ids.extract::<Vec<Py<PyAny>>>()?;
    if objects.len() != ids.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "glyph box and id counts differ",
        ));
    }
    if objects
        .iter()
        .flatten()
        .all(|value| value.bind(py).is_exact_instance_of::<PyFloat>())
    {
        if let (Ok(boxes), Ok(indices)) = (
            boxes.extract::<Vec<Box4>>(),
            ids.iter()
                .map(|id| id.bind(py).extract::<usize>())
                .collect::<PyResult<Vec<_>>>(),
        ) {
            if let Some(rows) = py.detach(|| tables::visual_rows(boxes, indices, median_height)) {
                return Ok(rows);
            }
        }
    }
    visual_rows_generic(py, &objects, &ids, median_height)
}

/// 读取几何对象但不把整数或自定义数值提前转为浮点，以保留原下标和算术语义。
pub(super) fn object_boxes(values: &Bound<'_, PyAny>) -> PyResult<Vec<Vec<Py<PyAny>>>> {
    values
        .try_iter()?
        .map(|value| {
            let value = value?;
            (0..4)
                .map(|axis| Ok(value.get_item(axis)?.unbind()))
                .collect()
        })
        .collect()
}

/// 以解释器稳定排序对来源索引排序，NaN、任意精度整数和平局仍按原输入顺序处理。
pub(super) fn sorted_object_indices(
    py: Python<'_>,
    keys: &Bound<'_, PyList>,
) -> PyResult<Vec<usize>> {
    let order = PyList::new(py, 0..keys.len())?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("key", keys.getattr("__getitem__")?)?;
    order.call_method("sort", (), Some(&kwargs))?;
    order.extract()
}

/// 特殊坐标的视觉行算法仍在 Rust 执行，原数值的加减、比较和 min/max 使用解释器算术。
fn visual_rows_generic(
    py: Python<'_>,
    boxes: &[Vec<Py<PyAny>>],
    ids: &[Py<PyAny>],
    height: f64,
) -> PyResult<Vec<Vec<usize>>> {
    let operators = py.import("operator")?;
    let builtins = py.import("builtins")?;
    let add = operators.getattr("add")?;
    let sub = operators.getattr("sub")?;
    let divide = operators.getattr("truediv")?;
    let absolute = operators.getattr("abs")?;
    let le = operators.getattr("le")?;
    let lt = operators.getattr("lt")?;
    let ge = operators.getattr("ge")?;
    let gt = operators.getattr("gt")?;
    let minimum = builtins.getattr("min")?;
    let maximum = builtins.getattr("max")?;
    let center = |bbox: &[Py<PyAny>]| -> PyResult<Py<PyAny>> {
        Ok(divide
            .call1((add.call1((bbox[1].bind(py), bbox[3].bind(py)))?, 2.0))?
            .unbind())
    };
    let keys = PyList::empty(py);
    for (bbox, id) in boxes.iter().zip(ids) {
        keys.append((center(bbox)?, bbox[0].bind(py), id.bind(py)))?;
    }
    let tolerance = maximum.call1((0.75, height * 0.40))?;
    let mut rows: Vec<Vec<usize>> = Vec::new();
    let mut bounds: Vec<Vec<Py<PyAny>>> = Vec::new();
    let mut prefix: Vec<Py<PyAny>> = Vec::new();
    for index in sorted_object_indices(py, &keys)? {
        let glyph = &boxes[index];
        let cy = center(glyph)?;
        let mut best = None;
        let mut best_distance = f64::INFINITY.into_pyobject(py)?.into_any().unbind();
        for row_index in (0..bounds.len()).rev() {
            let row = &bounds[row_index];
            let ry = center(row)?;
            let distance = absolute.call1((sub.call1((cy.bind(py), ry.bind(py)))?,))?;
            let overlap = sub.call1((
                minimum.call1((glyph[3].bind(py), row[3].bind(py)))?,
                maximum.call1((glyph[1].bind(py), row[1].bind(py)))?,
            ))?;
            let minimum_height = minimum.call1((
                sub.call1((glyph[3].bind(py), glyph[1].bind(py)))?,
                sub.call1((row[3].bind(py), row[1].bind(py)))?,
            ))?;
            let ratio = if le.call1((&overlap, 0))?.is_truthy()?
                || le.call1((&minimum_height, 0))?.is_truthy()?
            {
                0.0_f64.into_pyobject(py)?.into_any()
            } else {
                minimum.call1((1.0, divide.call1((overlap, minimum_height))?))?
            };
            if (ge.call1((ratio, 0.45))?.is_truthy()?
                || le.call1((&distance, &tolerance))?.is_truthy()?)
                && lt.call1((&distance, best_distance.bind(py)))?.is_truthy()?
            {
                best = Some(row_index);
                best_distance = distance.unbind();
            }
            if ge.call1((cy.bind(py), ry.bind(py)))?.is_truthy()?
                && gt
                    .call1((sub.call1((cy.bind(py), ry.bind(py)))?, &tolerance))?
                    .is_truthy()?
                && gt
                    .call1((glyph[1].bind(py), prefix[row_index].bind(py)))?
                    .is_truthy()?
            {
                break;
            }
        }
        if let Some(row_index) = best {
            rows[row_index].push(index);
            for axis in 0..4 {
                bounds[row_index][axis] = (if axis < 2 { &minimum } else { &maximum })
                    .call1((bounds[row_index][axis].bind(py), glyph[axis].bind(py)))?
                    .unbind();
            }
            for position in row_index..bounds.len() {
                prefix[position] = if position == 0 {
                    bounds[position][3].clone_ref(py)
                } else {
                    maximum
                        .call1((prefix[position - 1].bind(py), bounds[position][3].bind(py)))?
                        .unbind()
                };
            }
        } else {
            rows.push(vec![index]);
            bounds.push(glyph.iter().map(|value| value.clone_ref(py)).collect());
            prefix.push(
                maximum
                    .call1((
                        prefix
                            .last()
                            .map(|v| v.bind(py))
                            .unwrap_or(glyph[3].bind(py)),
                        glyph[3].bind(py),
                    ))?
                    .unbind(),
            );
        }
    }
    let row_keys = PyList::empty(py);
    for bbox in &bounds {
        row_keys.append((bbox[1].bind(py), bbox[0].bind(py)))?;
    }
    let mut output = Vec::new();
    for row_index in sorted_object_indices(py, &row_keys)? {
        let row = &rows[row_index];
        let keys = PyList::empty(py);
        for &index in row {
            keys.append((
                boxes[index][0].bind(py),
                boxes[index][1].bind(py),
                ids[index].bind(py),
            ))?;
        }
        output.push(
            sorted_object_indices(py, &keys)?
                .into_iter()
                .map(|index| row[index])
                .collect(),
        );
    }
    Ok(output)
}

/// 已物化的字符中心按整表轨道批量统计，不改变候选数量。
#[pyfunction]
pub(super) fn table_row_occupancy(
    py: Python<'_>,
    rows: Vec<Vec<f64>>,
    tracks: Vec<f64>,
) -> Option<Vec<Vec<usize>>> {
    py.detach(move || tables::row_occupancy(rows, tracks))
}

/// 表格区域一次筛选整页字符，特殊输入继续沿 Python 的原验证入口处理。
#[pyfunction]
pub(super) fn table_boxes(
    py: Python<'_>,
    values: &Bound<'_, PyList>,
    table: Box4,
    angle: i32,
    fallback: &Bound<'_, PyAny>,
) -> PyResult<Option<Vec<(usize, BoxTuple, Option<BoxTuple>)>>> {
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
pub(super) fn coverage_batch(
    py: Python<'_>,
    rules: Vec<tables::Rule>,
    queries: Vec<tables::Query>,
) -> Option<Vec<f64>> {
    py.detach(move || tables::coverage_batch(rules, queries))
}

/// 按 Python 簇坐标批量合并相邻线段。
#[pyfunction]
pub(super) fn merge_rules(
    py: Python<'_>,
    rules: Vec<tables::Rule>,
    coordinates: Vec<(u8, Vec<f64>)>,
    tolerance: f64,
    join: f64,
) -> PyResult<Vec<tables::Rule>> {
    if rules
        .iter()
        .all(|rule| [rule.1, rule.2, rule.3].iter().all(|v| v.is_finite()))
        && coordinates
            .iter()
            .flat_map(|(_, values)| values)
            .all(|v| v.is_finite())
    {
        return Ok(py
            .detach(move || tables::merge_rules(rules, coordinates, tolerance, join))
            .unwrap());
    }
    // 非有限区间沿用解释器稳定排序及首项 min/max，避免 Rust 全序排序改写 NaN 位置。
    let minimum = |a: f64, b: f64| if b < a { b } else { a };
    let maximum = |a: f64, b: f64| if b > a { b } else { a };
    let mut output = Vec::new();
    for (orientation, positions) in coordinates {
        for position in positions {
            let intervals = PyList::new(
                py,
                rules
                    .iter()
                    .filter(|rule| rule.0 == orientation && (rule.1 - position).abs() <= tolerance)
                    .map(|rule| (minimum(rule.2, rule.3), maximum(rule.2, rule.3)))
                    .collect::<Vec<_>>(),
            )?;
            intervals.call_method0("sort")?;
            let intervals = intervals.extract::<Vec<(f64, f64)>>()?;
            let Some(&(mut start, mut end)) = intervals.first() else {
                continue;
            };
            for &(left, right) in &intervals[1..] {
                if left <= end + join {
                    end = maximum(end, right);
                } else {
                    output.push((orientation, position, start, end));
                    start = left;
                    end = right;
                }
            }
            output.push((orientation, position, start, end));
        }
    }
    Ok(output)
}

/// 表格轴坐标以当前解释器 sum 的均值聚类，保留非有限值的稳定排序和邻簇规则。
#[pyfunction]
pub(super) fn table_cluster_positions(
    py: Python<'_>,
    values: Vec<f64>,
    tolerance: f64,
) -> PyResult<Vec<f64>> {
    let ordered = PyList::new(py, values)?;
    ordered.call_method0("sort")?;
    let sum = py.import("builtins")?.getattr("sum")?;
    let mut groups: Vec<Vec<f64>> = Vec::new();
    for value in ordered.extract::<Vec<f64>>()? {
        let append = if let Some(group) = groups.last() {
            let mean =
                sum.call1((PyList::new(py, group)?,))?.extract::<f64>()? / group.len() as f64;
            (value - mean).abs() <= tolerance
        } else {
            false
        };
        if append {
            groups.last_mut().unwrap().push(value);
        } else {
            groups.push(vec![value]);
        }
    }
    groups
        .iter()
        .map(|group| {
            Ok(sum.call1((PyList::new(py, group)?,))?.extract::<f64>()? / group.len() as f64)
        })
        .collect()
}

/// 批量完成所有字符的单元格分配，保留索引和歧义位。
#[pyfunction]
pub(super) fn assign_cells(
    py: Python<'_>,
    glyphs: Vec<Box4>,
    specs: Vec<Box4>,
    index: Option<tables::GridIndex>,
) -> PyResult<Vec<(Option<usize>, bool)>> {
    if specs.is_empty() {
        return Err(pyo3::exceptions::PyIndexError::new_err(
            "table has no cell specifications",
        ));
    }
    py.detach(move || tables::assign_cells(glyphs, specs, index))
        .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("invalid table grid index"))
}

/// 验证原子格索引后一次执行网格连接。
#[pyfunction]
pub(super) fn grid_parents(
    py: Python<'_>,
    count: usize,
    pairs: Vec<(usize, usize)>,
) -> PyResult<Vec<usize>> {
    if pairs.iter().any(|(a, b)| *a >= count || *b >= count) {
        return Err(pyo3::exceptions::PyIndexError::new_err(
            "cell index out of range",
        ));
    }
    Ok(py.detach(move || tables::grid_parents(count, pairs)))
}

/// 将合法并查集转为矩形单元格，同时返回路径压缩后的父节点。
#[pyfunction]
pub(super) fn component_specs(
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

/// 只读普通冻结字形的当前几何，特殊对象整体回退；计算阶段使用自有数组。
#[pyfunction]
pub(super) fn cell_visual_groups_owned(
    py: Python<'_>,
    glyphs: &Bound<'_, PyAny>,
    height: f64,
    glyph_type: &Bound<'_, PyAny>,
) -> PyResult<Vec<Vec<usize>>> {
    use pyo3::types::{PyFloat, PyInt, PyList, PyTuple};
    let Ok(glyphs) = glyphs.cast_exact::<PyList>() else {
        return cell_visual_groups_generic(py, glyphs, height);
    };
    let mut boxes = Vec::with_capacity(glyphs.len());
    let mut ids = Vec::with_capacity(glyphs.len());
    let mut visual = Vec::with_capacity(glyphs.len());
    for glyph in glyphs.iter() {
        if !glyph.get_type().is(glyph_type) {
            return cell_visual_groups_generic(py, glyphs, height);
        }
        let raw_id = glyph.getattr(pyo3::intern!(py, "glyph_id"))?;
        let raw_row = glyph.getattr(pyo3::intern!(py, "visual_row"))?;
        if !raw_id.is_exact_instance_of::<PyInt>() || !raw_row.is_exact_instance_of::<PyInt>() {
            return cell_visual_groups_generic(py, glyphs, height);
        }
        let (Ok(id), Ok(row)) = (raw_id.extract::<i64>(), raw_row.extract::<i64>()) else {
            return cell_visual_groups_generic(py, glyphs, height);
        };
        if id < 0 {
            return cell_visual_groups_generic(py, glyphs, height);
        }
        let bbox = glyph.getattr(pyo3::intern!(py, "bbox"))?;
        if (!bbox.is_exact_instance_of::<PyList>() && !bbox.is_exact_instance_of::<PyTuple>())
            || bbox.len()? != 4
        {
            return cell_visual_groups_generic(py, glyphs, height);
        }
        let mut b = [0.0; 4];
        for (i, value) in b.iter_mut().enumerate() {
            let number = bbox.get_item(i)?;
            if !number.is_exact_instance_of::<PyFloat>() {
                return cell_visual_groups_generic(py, glyphs, height);
            }
            *value = number.extract()?;
        }
        boxes.push(b);
        ids.push(id as usize);
        visual.push(row);
    }
    match py.detach(move || docvortex_core::tables::cell_visual_groups(boxes, ids, visual, height))
    {
        Some(groups) => Ok(groups),
        None => cell_visual_groups_generic(py, glyphs, height),
    }
}

/// 普通快照之外仍由原生层读取当前字形并细分视觉行，保留任意精度来源编号与整数坐标。
fn cell_visual_groups_generic(
    py: Python<'_>,
    glyphs: &Bound<'_, PyAny>,
    height: f64,
) -> PyResult<Vec<Vec<usize>>> {
    let operators = py.import("operator")?;
    let builtins = py.import("builtins")?;
    let add = operators.getattr("add")?;
    let sub = operators.getattr("sub")?;
    let divide = operators.getattr("truediv")?;
    let le = operators.getattr("le")?;
    let minimum = builtins.getattr("min")?;
    let maximum = builtins.getattr("max")?;
    let median = py.import("statistics")?.getattr("median")?;
    let group_ids = PyDict::new(py);
    let group_keys = PyList::empty(py);
    let mut groups: Vec<Vec<usize>> = Vec::new();
    let mut boxes: Vec<Vec<Py<PyAny>>> = Vec::new();
    let mut ids: Vec<Py<PyAny>> = Vec::new();
    for glyph in glyphs.try_iter()? {
        let glyph = glyph?;
        let visual = glyph.getattr("visual_row")?;
        let index = if let Some(index) = group_ids.get_item(&visual)? {
            index.extract::<usize>()?
        } else {
            let index = groups.len();
            group_ids.set_item(&visual, index)?;
            group_keys.append(visual)?;
            groups.push(Vec::new());
            index
        };
        groups[index].push(boxes.len());
        let bbox = glyph.getattr("bbox")?;
        boxes.push(
            (0..4)
                .map(|axis| Ok(bbox.get_item(axis)?.unbind()))
                .collect::<PyResult<Vec<_>>>()?,
        );
        ids.push(glyph.getattr("glyph_id")?.unbind());
    }
    let mut output = Vec::new();
    for group_index in sorted_object_indices(py, &group_keys)? {
        let original = &groups[group_index];
        let local_boxes: Vec<_> = original
            .iter()
            .map(|&i| boxes[i].iter().map(|v| v.clone_ref(py)).collect())
            .collect();
        let local_ids: Vec<_> = original.iter().map(|&i| ids[i].clone_ref(py)).collect();
        let local = visual_rows_generic(py, &local_boxes, &local_ids, height)?;
        let mut coherent = true;
        for row in &local {
            let centers = PyList::empty(py);
            for &i in row {
                let bbox = &local_boxes[i];
                centers.append(
                    divide.call1((add.call1((bbox[1].bind(py), bbox[3].bind(py)))?, 2.0))?,
                )?;
            }
            let spread = sub.call1((maximum.call1((&centers,))?, minimum.call1((&centers,))?))?;
            if !le.call1((spread, 0.3 * height))?.is_truthy()? {
                coherent = false;
                break;
            }
        }
        let mut split = local.len() > 1 && coherent;
        if split {
            for row in &local {
                if row.len() < 2 {
                    split = false;
                    break;
                }
                let heights = PyList::empty(py);
                for &i in row {
                    heights.append(
                        sub.call1((local_boxes[i][3].bind(py), local_boxes[i][1].bind(py)))?,
                    )?;
                }
                let middle = median.call1((heights,))?;
                if !le.call1((0.85 * height, &middle))?.is_truthy()?
                    || !le.call1((&middle, 1.25 * height))?.is_truthy()?
                {
                    split = false;
                    break;
                }
            }
        }
        if split {
            output.extend(
                local
                    .into_iter()
                    .map(|row| row.into_iter().map(|i| original[i]).collect()),
            );
        } else {
            output.push(original.clone());
        }
    }
    Ok(output)
}
