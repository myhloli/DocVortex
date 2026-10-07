//! 图形数值批量绑定，返回布尔标志而不重复物化框坐标。
use pyo3::prelude::*;

/// 完整几何快照批量匹配，纯数值自有缓冲在释放 GIL 后计算，不访问源对象。
#[pyfunction]
pub(super) fn overlap_member_groups(
    py: Python<'_>,
    members: Vec<[f64; 4]>,
    regions: Vec<[f64; 4]>,
    threshold: f64,
) -> Option<Vec<Vec<usize>>> {
    if !threshold.is_finite()
        || members
            .iter()
            .chain(&regions)
            .flatten()
            .any(|v| !v.is_finite() || v.abs() > 2f64.powi(50))
    {
        return None;
    }
    Some(py.detach(|| {
        docvortex_core::rule_graphics::overlap_member_groups(&members, &regions, threshold)
    }))
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

/// 文字特征仅计算一次；完整邻接表不替代 Python 的公式认领和 canonical 重放。
#[pyfunction]
pub(super) fn detached_formula_neighbors(
    py: Python<'_>,
    boxes: Vec<[f64; 4]>,
    operators: Vec<bool>,
    em: f64,
) -> Option<Vec<Vec<usize>>> {
    if boxes.len() != operators.len()
        || boxes.len() > 2000
        || !em.is_finite()
        || boxes
            .iter()
            .flatten()
            .any(|v| !v.is_finite() || v.abs() > 2f64.powi(50))
    {
        return None;
    }
    Some(
        py.detach(|| docvortex_core::rule_text::detached_formula_neighbors(&boxes, &operators, em)),
    )
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
    drafts: Vec<docvortex_core::rule_text::ProseRuleDraft>,
    grids: Vec<[f64; 4]>,
    rules: Vec<([f64; 4], bool)>,
    lines: Vec<docvortex_core::rule_text::ProseRuleLine>,
) -> Option<Vec<bool>> {
    let finite = |v: &f64| v.is_finite() && v.abs() <= 2f64.powi(50);
    if drafts
        .iter()
        .any(|d| d.0 <= 0.0 || ![d.0, d.1, d.2].iter().all(finite) || !d.3.iter().all(finite))
        || !grids.iter().flatten().all(finite)
        || rules.iter().any(|r| !r.0.iter().all(finite))
        || lines.iter().any(|r| !r.0.iter().all(finite))
    {
        return None;
    }
    Some(py.detach(|| {
        docvortex_core::rule_text::prose_rule_conflicts(&drafts, &grids, &rules, &lines)
    }))
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
    lines: Vec<[f64; 4]>,
    rules: Vec<[f64; 4]>,
    em: f64,
) -> Option<Vec<Vec<usize>>> {
    if !em.is_finite()
        || em <= 0.0
        || lines
            .iter()
            .chain(&rules)
            .flatten()
            .any(|v| !v.is_finite() || v.abs() > 2f64.powi(50))
    {
        return None;
    }
    Some(py.detach(|| docvortex_core::rule_graphics::year_header_groups(&lines, &rules, em)))
}
