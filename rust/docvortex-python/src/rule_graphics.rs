//! 图形数值批量绑定，返回布尔标志而不重复物化框坐标。
use pyo3::prelude::*;

/// 完整几何快照批量匹配，纯数值自有缓冲在释放 GIL 后计算，不访问源对象。
#[pyfunction]
pub(super) fn overlap_member_groups(
    py: Python<'_>,
    members: &Bound<'_, PyAny>,
    regions: &Bound<'_, PyAny>,
    threshold: f64,
) -> PyResult<Vec<Vec<usize>>> {
    let member_objects = super::tables::object_boxes(members)?;
    let region_objects = super::tables::object_boxes(regions)?;
    if ordinary_table_boxes(py, &member_objects)
        && ordinary_table_boxes(py, &region_objects)
        && threshold.is_finite()
    {
        let members = members.extract::<Vec<[f64; 4]>>()?;
        let regions = regions.extract::<Vec<[f64; 4]>>()?;
        return Ok(py.detach(|| {
            docvortex_core::rule_graphics::overlap_member_groups(&members, &regions, threshold)
        }));
    }
    let operators = py.import("operator")?;
    let builtins = py.import("builtins")?;
    let sub = operators.getattr("sub")?;
    let multiply = operators.getattr("mul")?;
    let divide = operators.getattr("truediv")?;
    let greater = operators.getattr("gt")?;
    let greater_equal = operators.getattr("ge")?;
    let minimum = builtins.getattr("min")?;
    let maximum = builtins.getattr("max")?;
    let mut output = Vec::new();
    for region in &region_objects {
        let mut selected = Vec::new();
        for (index, bbox) in member_objects.iter().enumerate() {
            let mut sides = Vec::new();
            let mut areas = Vec::new();
            for axis in 0..2 {
                sides.push(maximum.call1((
                    0.0,
                    sub.call1((
                        minimum.call1((bbox[axis + 2].bind(py), region[axis + 2].bind(py)))?,
                        maximum.call1((bbox[axis].bind(py), region[axis].bind(py)))?,
                    ))?,
                ))?);
                areas.push(maximum.call1((
                    0.0,
                    sub.call1((bbox[axis + 2].bind(py), bbox[axis].bind(py)))?,
                ))?);
            }
            let area = multiply.call1((&areas[0], &areas[1]))?;
            let ratio = if greater.call1((&area, 0))?.is_truthy()? {
                divide.call1((multiply.call1((&sides[0], &sides[1]))?, area))?
            } else {
                0.0_f64.into_pyobject(py)?.into_any()
            };
            if greater_equal.call1((ratio, threshold))?.is_truthy()? {
                selected.push(index);
            }
        }
        output.push(selected);
    }
    Ok(output)
}

/// 普通有限浮点继续使用无 GIL 批量内核，其他合法数值交由原生通用算术保持精度。
fn ordinary_table_boxes(py: Python<'_>, boxes: &[Vec<Py<PyAny>>]) -> bool {
    boxes.iter().flatten().all(|value| {
        value
            .bind(py)
            .is_exact_instance_of::<pyo3::types::PyFloat>()
            && value
                .bind(py)
                .extract::<f64>()
                .is_ok_and(|value| value.is_finite() && value.abs() <= 2f64.powi(50))
    })
}

/// 普通有限几何批量处理完整横线对，异常数值完整回退到原始规则循环。
#[pyfunction]
pub(super) fn code_rule_windows(
    py: Python<'_>,
    horizontal: Vec<[f64; 4]>,
    vertical: Vec<[f64; 4]>,
    lines: Vec<[f64; 4]>,
    excluded: Vec<[f64; 4]>,
    em: f64,
    page_height: f64,
) -> Option<Vec<docvortex_core::rule_graphics::CodeRuleWindow>> {
    if !em.is_finite()
        || !page_height.is_finite()
        || em <= 0.0
        || page_height <= 0.0
        || horizontal
            .iter()
            .chain(&vertical)
            .chain(&lines)
            .chain(&excluded)
            .flatten()
            .any(|value| !value.is_finite() || value.abs() > 2f64.powi(50))
    {
        return None;
    }
    Some(py.detach(|| {
        docvortex_core::rule_graphics::code_rule_windows(
            &horizontal,
            &vertical,
            &lines,
            &excluded,
            em,
            page_height,
        )
    }))
}

/// 仅物化分量索引，候选数超过旧邻接表上限时仍在原生内核完成稳定扫描。
#[pyfunction]
pub(super) fn detached_formula_component(
    py: Python<'_>,
    boxes: Vec<[f64; 4]>,
    operators: Vec<bool>,
    em: f64,
    available: Vec<usize>,
) -> PyResult<Vec<usize>> {
    if boxes.len() != operators.len() || available.iter().any(|index| *index >= boxes.len()) {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "formula boxes and operator flags must have equal lengths",
        ));
    }
    Ok(py.detach(|| {
        docvortex_core::rule_text::detached_formula_component(&boxes, &operators, em, &available)
    }))
}

/// 一次传入本页所有未缓存复合路径，纯数值计算可释放 GIL。
#[pyfunction]
pub(super) fn compound_baselines(
    py: Python<'_>,
    groups: Vec<Vec<[f64; 4]>>,
    em: f64,
) -> Vec<(bool, bool)> {
    py.detach(|| docvortex_core::rule_graphics::compound_baselines(&groups, em))
}

/// 只借用不可变内容字节，返回原始操作数区间而不创建无关 PDF 对象。
#[pyfunction]
pub(super) fn form_state_events(
    py: Python<'_>,
    data: Vec<u8>,
) -> Option<Vec<docvortex_core::form_events::FormEvent>> {
    py.detach(|| docvortex_core::form_events::form_state_events(&data))
}

/// 数字转换批量完成，资源名称保留原始区间以沿用 pypdf 的解码语义。
#[pyfunction]
pub(super) fn form_state_program(
    py: Python<'_>,
    data: Vec<u8>,
) -> Option<Vec<docvortex_core::form_events::FormProgramEvent>> {
    py.detach(|| docvortex_core::form_events::form_state_program(&data))
}

/// 一次处理数字噪声的全部几何候选，输出稳定的源行索引。
#[pyfunction]
pub(super) fn numeric_noise_candidates(
    py: Python<'_>,
    lines: Vec<docvortex_core::rule_text::NoiseLine>,
    visual: Vec<[f64; 4]>,
) -> Vec<(usize, Vec<usize>)> {
    py.detach(|| docvortex_core::rule_text::numeric_noise_candidates(&lines, &visual))
}

/// 原始数值和几何一次传入，返回通过规则的刻度源索引与实际中位字号。
#[pyfunction]
pub(super) fn raster_axis_groups(
    py: Python<'_>,
    lines: Vec<([f64; 4], f64, f64)>,
) -> Vec<(bool, Vec<usize>, f64)> {
    py.detach(|| docvortex_core::rule_graphics::raster_axis_groups(&lines))
}

/// 只迁移公式的连通扫描，种子选择和业务屏障由 Python 保留。
#[pyfunction]
pub(super) fn grow_formula_component(
    py: Python<'_>,
    lines: Vec<docvortex_core::rule_text::FormulaLine>,
    seed_count: usize,
    tables: Vec<[f64; 4]>,
) -> Vec<usize> {
    py.detach(|| docvortex_core::rule_text::grow_formula_component(&lines, seed_count, &tables))
}

/// 整栏事件一次扫描，只返回需要迁移的行和目标栏，Python 继续持有原始对象。
#[pyfunction]
pub(super) fn short_tail_destinations(
    py: Python<'_>,
    lines: Vec<docvortex_core::rule_text::TailLine>,
    lanes: Vec<(f64, f64)>,
    median: f64,
) -> Vec<(usize, usize)> {
    py.detach(|| docvortex_core::rule_text::short_tail_destinations(&lines, &lanes, median))
}

/// 路径几何一次分组；图体最终成员与文本过滤仍由原有阶段独立计算。
#[pyfunction]
pub(super) fn isolated_path_groups(
    py: Python<'_>,
    boxes: Vec<[f64; 4]>,
    em: f64,
) -> Option<Vec<Vec<usize>>> {
    py.detach(|| docvortex_core::rule_graphics::isolated_path_groups(&boxes, em))
}

/// 只使用自有数值做包含计算，不在释放 GIL 时访问 PDFium 或 Python 页面对象。
#[pyfunction]
pub(super) fn overpainted_addresses(
    py: Python<'_>,
    texts: Vec<(usize, usize, [f64; 4])>,
    covers: Vec<(usize, [f64; 4])>,
) -> Vec<usize> {
    py.detach(|| docvortex_core::rule_graphics::overpainted_addresses(&texts, &covers))
}

/// Rust 只处理分组循环，调用当前 Python 的 fmean 保留精确舍入；最终框和行中心仍由 Python 生成。
#[pyfunction]
pub(super) fn fragment_row_groups(
    items: Vec<(f64, f64, Option<i64>)>,
    tolerance: f64,
    mean: &Bound<'_, PyAny>,
) -> PyResult<Option<Vec<Vec<usize>>>> {
    if !tolerance.is_finite()
        || items.iter().any(|item| {
            !item.0.is_finite()
                || !item.1.is_finite()
                || item.0.abs().max(item.1.abs()) > 2f64.powi(50)
        })
    {
        return Ok(None);
    }
    let groups = docvortex_core::rule_text::fragment_row_groups(&items, tolerance, |indices| {
        // fsum 对单个负零也返回正零；普通单元素均值不需要 Python 回调。
        if indices.len() == 1 {
            let value = items[indices[0]].0;
            Ok(if value == 0.0 { 0.0 } else { value })
        } else {
            let values: Vec<_> = indices.iter().map(|&i| items[i].0).collect();
            mean.call1((values,))?.extract::<f64>()
        }
    })?;
    Ok(Some(groups))
}

/// 使用自有快照处理全部弱横线候选，几何缓存仅限此次调用；返回逐候选冲突标志。
#[pyfunction]
pub(super) fn prose_rule_conflicts(
    py: Python<'_>,
    drafts: &Bound<'_, PyAny>,
    grids: &Bound<'_, PyAny>,
    rules: &Bound<'_, PyAny>,
    lines: &Bound<'_, PyAny>,
) -> PyResult<Vec<bool>> {
    type Draft = (Py<PyAny>, Py<PyAny>, Py<PyAny>, Vec<Py<PyAny>>, Vec<i64>);
    let draft_objects = drafts.extract::<Vec<Draft>>()?;
    let grid_objects = super::tables::object_boxes(grids)?;
    let rule_objects = rules.extract::<Vec<(Vec<Py<PyAny>>, bool)>>()?;
    let line_objects = lines.extract::<Vec<(Vec<Py<PyAny>>, i64, bool)>>()?;
    let all_boxes: Vec<_> = draft_objects
        .iter()
        .map(|draft| &draft.3)
        .chain(grid_objects.iter())
        .chain(rule_objects.iter().map(|rule| &rule.0))
        .chain(line_objects.iter().map(|line| &line.0))
        .collect();
    if all_boxes.iter().any(|bbox| bbox.len() < 4) {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "table rule boxes need four coordinates",
        ));
    }
    let ordinary = |value: &Py<PyAny>| {
        value
            .bind(py)
            .is_exact_instance_of::<pyo3::types::PyFloat>()
            && value
                .bind(py)
                .extract::<f64>()
                .is_ok_and(|value| value.is_finite() && value.abs() <= 2f64.powi(50))
    };
    if all_boxes
        .iter()
        .all(|bbox| bbox.len() == 4 && bbox.iter().all(ordinary))
        && draft_objects
            .iter()
            .all(|draft| [&draft.0, &draft.1, &draft.2].into_iter().all(ordinary))
    {
        let drafts = drafts.extract::<Vec<docvortex_core::rule_text::ProseRuleDraft>>()?;
        let grids = grids.extract::<Vec<[f64; 4]>>()?;
        let rules = rules.extract::<Vec<([f64; 4], bool)>>()?;
        let lines = lines.extract::<Vec<docvortex_core::rule_text::ProseRuleLine>>()?;
        return Ok(py.detach(|| {
            docvortex_core::rule_text::prose_rule_conflicts(&drafts, &grids, &rules, &lines)
        }));
    }
    // 特殊尺度和整数坐标在 Rust 循环内使用宿主数值运算，避免先缩窄为 f64 改写候选边界。
    let operators = py.import("operator")?;
    let builtins = py.import("builtins")?;
    let minimum = builtins.getattr("min")?;
    let maximum = builtins.getattr("max")?;
    macro_rules! calculate {
        ($operation:expr, $first:expr, $second:expr) => {
            operators.getattr($operation)?.call1(($first, $second))?
        };
    }
    macro_rules! compare {
        ($operation:expr, $first:expr, $second:expr) => {
            calculate!($operation, $first, $second).is_truthy()?
        };
    }
    let center = |bbox: &[Py<PyAny>], axis: usize| -> PyResult<Bound<'_, PyAny>> {
        Ok(calculate!(
            "truediv",
            calculate!("add", bbox[axis].bind(py), bbox[axis + 2].bind(py)),
            2.0
        ))
    };
    let axis_overlap = |first: &[Py<PyAny>], second: &[Py<PyAny>]| -> PyResult<f64> {
        let overlap = maximum.call1((
            0.0,
            calculate!(
                "sub",
                minimum.call1((first[2].bind(py), second[2].bind(py)))?,
                maximum.call1((first[0].bind(py), second[0].bind(py)))?
            ),
        ))?;
        let shorter = minimum.call1((
            calculate!("sub", first[2].bind(py), first[0].bind(py)),
            calculate!("sub", second[2].bind(py), second[0].bind(py)),
        ))?;
        if compare!("gt", &shorter, 0) {
            calculate!("truediv", overlap, shorter).extract()
        } else {
            Ok(0.0)
        }
    };
    let mut output = Vec::new();
    for (em, first, last, boundary, members) in &draft_objects {
        let members: std::collections::HashSet<_> = members.iter().copied().collect();
        let mut conflict = false;
        for core in &grid_objects {
            let width = calculate!("sub", core[2].bind(py), core[0].bind(py));
            let height = calculate!("sub", core[3].bind(py), core[1].bind(py));
            if compare!("lt", &width, calculate!("mul", 8, em.bind(py)))
                || compare!("lt", height, calculate!("mul", 4, em.bind(py)))
                || compare!(
                    "lt",
                    calculate!("sub", core[1].bind(py), first.bind(py)),
                    calculate!("mul", 5, em.bind(py))
                )
                || !compare!(
                    "le",
                    calculate!("sub", core[1].bind(py), em.bind(py)),
                    last.bind(py)
                )
                || !compare!(
                    "le",
                    last.bind(py),
                    calculate!("add", core[3].bind(py), em.bind(py))
                )
                || axis_overlap(boundary, core)? < 0.9
            {
                continue;
            }
            let verticals = pyo3::types::PyList::empty(py);
            for (bbox, vertical) in &rule_objects {
                if !vertical {
                    continue;
                }
                let x = center(bbox, 0)?;
                if compare!(
                    "le",
                    calculate!("sub", core[0].bind(py), calculate!("mul", 0.5, em.bind(py))),
                    &x
                ) && compare!(
                    "le",
                    &x,
                    calculate!("add", core[2].bind(py), calculate!("mul", 0.5, em.bind(py)))
                ) && compare!(
                    "le",
                    bbox[1].bind(py),
                    calculate!("add", core[1].bind(py), calculate!("mul", 0.5, em.bind(py)))
                ) && compare!(
                    "ge",
                    bbox[3].bind(py),
                    calculate!("sub", core[3].bind(py), calculate!("mul", 0.5, em.bind(py)))
                ) {
                    verticals.append(x)?;
                }
            }
            verticals.call_method0("sort")?;
            let mut tracks: Vec<Py<PyAny>> = Vec::new();
            for x in verticals.iter() {
                let separated = if let Some(previous) = tracks.last() {
                    compare!(
                        "gt",
                        calculate!("sub", &x, previous.bind(py)),
                        calculate!("mul", 0.3, em.bind(py))
                    )
                } else {
                    true
                };
                if separated {
                    tracks.push(x.unbind());
                }
            }
            if tracks.len() < 3 {
                continue;
            }
            let mut enclosed = true;
            for y in [&core[1], &core[3]] {
                let mut found = false;
                for (bbox, vertical) in &rule_objects {
                    if !vertical && axis_overlap(bbox, core)? >= 0.9 {
                        let gap = operators.getattr("abs")?.call1((calculate!(
                            "sub",
                            center(bbox, 1)?,
                            y.bind(py)
                        ),))?;
                        if compare!("le", gap, calculate!("mul", 0.5, em.bind(py))) {
                            found = true;
                            break;
                        }
                    }
                }
                if !found {
                    enclosed = false;
                    break;
                }
            }
            if !enclosed {
                continue;
            }
            let centers = pyo3::types::PyList::empty(py);
            for (bbox, source, eligible) in &line_objects {
                if !eligible || !members.contains(source) {
                    continue;
                }
                let y = center(bbox, 1)?;
                if compare!(
                    "lt",
                    calculate!("add", first.bind(py), calculate!("mul", 0.5, em.bind(py))),
                    &y
                ) && compare!("lt", &y, calculate!("sub", core[1].bind(py), em.bind(py)))
                    && compare!(
                        "ge",
                        calculate!("sub", bbox[2].bind(py), bbox[0].bind(py)),
                        calculate!("mul", 0.65, &width)
                    )
                    && axis_overlap(bbox, core)? >= 0.9
                {
                    centers.append(y)?;
                }
            }
            centers.call_method0("sort")?;
            let mut rows: Vec<Py<PyAny>> = Vec::new();
            for y in centers.iter() {
                let separated = if let Some(previous) = rows.last() {
                    compare!(
                        "gt",
                        calculate!("sub", &y, previous.bind(py)),
                        calculate!("mul", 0.6, em.bind(py))
                    )
                } else {
                    true
                };
                if separated {
                    rows.push(y.unbind());
                }
            }
            if rows.len() >= 3 {
                conflict = true;
                break;
            }
        }
        output.push(conflict);
    }
    Ok(output)
}

/// 不复制源成员地批量检查全部必要几何条件，为后续完整正文判断选择输入。
#[pyfunction]
pub(super) fn prose_rule_eligible(
    py: Python<'_>,
    drafts: Vec<docvortex_core::rule_text::ProseRuleDraft>,
    grids: Vec<[f64; 4]>,
) -> Vec<bool> {
    py.detach(|| docvortex_core::rule_text::prose_rule_eligible(&drafts, &grids))
}

/// 完整普通几何在独立缓冲中匹配，异常尺度或坐标完整交回原表头规则。
#[pyfunction]
pub(super) fn year_header_groups(
    py: Python<'_>,
    lines: &Bound<'_, PyAny>,
    rules: &Bound<'_, PyAny>,
    em: f64,
) -> PyResult<Vec<Vec<usize>>> {
    let line_objects = super::tables::object_boxes(lines)?;
    let rule_objects = super::tables::object_boxes(rules)?;
    if ordinary_table_boxes(py, &line_objects)
        && ordinary_table_boxes(py, &rule_objects)
        && em.is_finite()
    {
        let lines = lines.extract::<Vec<[f64; 4]>>()?;
        let rules = rules.extract::<Vec<[f64; 4]>>()?;
        return Ok(
            py.detach(|| docvortex_core::rule_graphics::year_header_groups(&lines, &rules, em))
        );
    }
    let operators = py.import("operator")?;
    let add = operators.getattr("add")?;
    let sub = operators.getattr("sub")?;
    let le = operators.getattr("le")?;
    let mut output = Vec::new();
    for rule in rule_objects {
        let mut selected = Vec::new();
        for (index, bbox) in line_objects.iter().enumerate() {
            if !le
                .call1((sub.call1((rule[0].bind(py), 0.3 * em))?, bbox[0].bind(py)))?
                .is_truthy()?
                || !le
                    .call1((bbox[2].bind(py), add.call1((rule[2].bind(py), 0.3 * em))?))?
                    .is_truthy()?
            {
                continue;
            }
            let gap = sub.call1((rule[1].bind(py), bbox[3].bind(py)))?;
            if le.call1((0, &gap))?.is_truthy()? && le.call1((gap, 0.8 * em))?.is_truthy()? {
                selected.push(index);
            }
        }
        output.push(selected);
    }
    Ok(output)
}
