//! 将 Python 批量参数转换为自有记录，再交给纯 Rust 核心计算。
// 绑定使用列式数组和原四元组输出，不为私有传输引入新的公开对象类型。
#![allow(clippy::too_many_arguments, clippy::type_complexity)]

use docvortex_core::dedup;
use docvortex_core::extraction;
use docvortex_core::geometry::{self, Box4, Size};
use docvortex_core::tables;
use pyo3::prelude::*;
use pyo3::types::{PyFloat, PyList, PyTuple};
mod pdfium;

/// 行范围极值索引，不向 Python 返回重复浮点坐标。
#[pyclass(frozen)]
struct TableRowGeometry {
    index: docvortex_core::row_geometry::RowGeometry,
}

#[pymethods]
impl TableRowGeometry {
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
struct StableColumnClusters {
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

/// 调用内持有只读区间树，不缓存 Python 行对象。
#[pyclass(frozen)]
struct BaselineCandidates {
    index: docvortex_core::spatial::IntervalIndex,
}

/// 为整页候选复用排序后的正文高度，查询仅传入区间及核心来源。
#[pyclass(frozen)]
struct TableNoteMetrics {
    metrics: std::sync::Arc<docvortex_core::statistics::NoteMetrics>,
}

/// 已验证走廊行的来源成员及其中心范围，只在当前候选组复用。
#[pyclass(frozen)]
struct TableNoteCore {
    rows: docvortex_core::note_index::CoreRows,
    owner: std::sync::Arc<docvortex_core::statistics::NoteMetrics>,
}

#[pymethods]
impl TableNoteMetrics {
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

#[pymethods]
impl BaselineCandidates {
    /// 验证区间后建立纯数值索引，拒绝不完整的分组参数。
    #[new]
    fn new(py: Python<'_>, bounds: Vec<(f64, f64)>, groups: Vec<usize>) -> PyResult<Self> {
        py.detach(move || docvortex_core::spatial::IntervalIndex::new(bounds, groups))
            .map(|index| Self { index })
            .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("invalid interval index"))
    }

    /// 按固定容量查询连续行，最密的一行也不会造成全页平方内存。
    fn rows(&self, py: Python<'_>, start: usize, count: usize, budget: usize) -> Vec<Vec<usize>> {
        py.detach(|| self.index.rows(start, count.min(64), budget.min(8192)))
    }
}

/// 在一次调用内过滤同行几何，保留原始行框的独立准入路径。
#[pyclass(frozen)]
struct BaselineGeometryCandidates {
    index: docvortex_core::spatial::BaselineGeometry,
}
#[pymethods]
impl BaselineGeometryCandidates {
    /// 创建验证后的只读索引，拒绝不支持的传输记录。
    #[new]
    fn new(
        py: Python<'_>,
        bounds: Vec<(f64, f64)>,
        groups: Vec<usize>,
        boxes: Vec<Box4>,
        heights: Vec<f64>,
        sources: Vec<Option<Box4>>,
    ) -> PyResult<Self> {
        py.detach(|| {
            docvortex_core::spatial::BaselineGeometry::new(bounds, groups, boxes, heights, sources)
        })
        .map(|index| Self { index })
        .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("unsupported baseline geometry"))
    }
    /// 返回当前原始行序的有限批次，不保存整页行对。
    fn rows(&self, py: Python<'_>, start: usize, count: usize, budget: usize) -> Vec<Vec<usize>> {
        py.detach(|| self.index.rows(start, count, budget))
    }
}

/// 批量聚类仅返回索引，避免为簇成员复制坐标与公开对象。
#[pyfunction]
fn ordered_clusters(
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
fn typography_metrics(
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
fn lane_gap(
    py: Python<'_>,
    boxes: Vec<Box4>,
    heights: Vec<f64>,
    restored: Vec<bool>,
    skip: Vec<bool>,
) -> Option<(f64, f64)> {
    py.detach(move || docvortex_core::statistics::lane_gap(boxes, heights, restored, skip))
}

/// 表格组行只返回成员索引，由 Python 复用原字形对象。
#[pyfunction]
fn table_visual_rows(
    py: Python<'_>,
    boxes: Vec<Box4>,
    ids: Vec<usize>,
    median_height: f64,
) -> Option<Vec<Vec<usize>>> {
    py.detach(move || tables::visual_rows(boxes, ids, median_height))
}

/// 已物化的字符中心按整表轨道批量统计，不改变候选数量。
#[pyfunction]
fn table_row_occupancy(
    py: Python<'_>,
    rows: Vec<Vec<f64>>,
    tracks: Vec<f64>,
) -> Option<Vec<Vec<usize>>> {
    py.detach(move || tables::row_occupancy(rows, tracks))
}

/// 在 PDFium 读取结束后批量转换数值，不接触句柄或同步锁。
#[pyfunction]
fn materialize_geometry(
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
/// 一次消费整行锚点，不回传重复坐标，只返回合格相邻对的统计量。
#[pyfunction]
fn anchor_pairs(
    py: Python<'_>,
    records: Vec<(usize, Box4, Box4, Size, f64)>,
    positive_source: bool,
) -> Option<Vec<(usize, f64, f64, bool)>> {
    py.detach(move || geometry::anchor_pairs(records, positive_source))
}
/// 冻结一页行级数值后批量计算上下物理净空。
#[pyfunction]
fn title_gaps(
    py: Python<'_>,
    records: Vec<(usize, Option<usize>, Box4, f64, bool)>,
) -> Option<Vec<(Option<f64>, Option<f64>)>> {
    py.detach(move || docvortex_core::spatial::title_gaps(records))
}

/// 返回邻行索引，供 Python 按原顺序追加原始分析对象。
#[pyfunction]
fn line_neighbors(
    py: Python<'_>,
    records: Vec<(usize, Box4, f64, f64)>,
) -> Option<Vec<(Option<usize>, Option<usize>)>> {
    py.detach(move || docvortex_core::spatial::line_neighbors(records))
}
/// 只传递一次每字符数值，返回分组范围以复用原有字符对象。
#[pyfunction]
fn mapping_runs(py: Python<'_>, records: Vec<dedup::MappingRecord>) -> Option<Vec<(usize, usize)>> {
    py.detach(move || dedup::mapping_runs(records))
}
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
fn source_rows<'py>(
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

/// 一次提取已物化的数值特征，释放 GIL 后计算整段字符角色。
#[pyfunction]
fn script_roles(py: Python<'_>, records: Vec<docvortex_core::scripts::Record>) -> Option<Vec<u8>> {
    py.detach(move || docvortex_core::scripts::classify(records))
}

/// 合并校验与分类边界，避免为每个字符创建两份中间 Python 坐标对象。
#[pyfunction]
fn script_roles_raw(
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

/// 注册私有扩展及协议号；公开 Python 接口仍由原模块提供。
#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(anchor_pairs, module)?)?;
    module.add_function(wrap_pyfunction!(title_gaps, module)?)?;
    module.add_function(wrap_pyfunction!(line_neighbors, module)?)?;
    module.add_function(wrap_pyfunction!(mapping_runs, module)?)?;
    module.add_function(wrap_pyfunction!(pdfium::read_pdfium_chars, module)?)?;
    module.add_function(wrap_pyfunction!(pdfium::read_pdfium_char_batches, module)?)?;
    module.add_class::<pdfium::PdfiumCharacterBatches>()?;
    module.add("PDFIUM_RECORD_BATCH_SIZE", pdfium::RECORD_BATCH_SIZE)?;
    module.add(
        "PdfiumReadError",
        module.py().get_type::<pdfium::PdfiumReadError>(),
    )?;
    module.add_class::<BaselineCandidates>()?;
    module.add_class::<TableNoteMetrics>()?;
    module.add_class::<StableColumnClusters>()?;
    module.add_class::<TableRowGeometry>()?;
    module.add_class::<BaselineGeometryCandidates>()?;
    module.add_function(wrap_pyfunction!(ordered_clusters, module)?)?;
    module.add_function(wrap_pyfunction!(typography_metrics, module)?)?;
    module.add_function(wrap_pyfunction!(lane_gap, module)?)?;
    module.add_function(wrap_pyfunction!(table_visual_rows, module)?)?;
    module.add_function(wrap_pyfunction!(table_row_occupancy, module)?)?;
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
    module.add_function(wrap_pyfunction!(materialize_geometry, module)?)?;
    module.add_function(wrap_pyfunction!(script_roles_raw, module)?)?;
    Ok(())
}
