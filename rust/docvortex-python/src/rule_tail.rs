//! 短尾几何的只读对象快照，避免适配层多次遍历相同字段。
// 高频字段名按解释器复用不可变字符串，仍由原 Python 属性和字典操作读取当前状态。
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyFloat, PyInt, PyList, PyString, PyTuple};
use std::collections::{HashMap, HashSet};

/// 只接受内置有限数值，特殊对象在转换回调发生之前交回 Python。
fn number(value: &Bound<'_, PyAny>) -> Option<f64> {
    if !value.is_exact_instance_of::<PyFloat>() && !value.is_exact_instance_of::<PyInt>() {
        return None;
    }
    let number = value.extract::<f64>().ok()?;
    (number.is_finite() && number.abs() <= 2f64.powi(50)).then_some(number)
}

/// 可缺省的内置数值保持 None，不能把未知值当作缺失字体或基线。
fn optional_number(value: &Bound<'_, PyAny>) -> Option<Option<f64>> {
    if value.is_none() {
        Some(None)
    } else {
        number(value).map(Some)
    }
}

/// 已经核验的行槽一次读取，避免 Python 再次构造、遍历数值校验列表。
fn pack_lines(pending: &Bound<'_, PyAny>) -> Option<Vec<docvortex_core::rule_text::TailLine>> {
    let mut records = Vec::new();
    let mut fonts = HashMap::<(String, i64), usize>::new();
    for item in pending.try_iter().ok()? {
        let item = item.ok()?;
        let source = item.get_item(0).ok()?.extract::<usize>().ok()?;
        let row = item.get_item(1).ok()?;
        let line = row.get_item(0).ok()?;
        let box_obj = row.get_item(1).ok()?;
        let mut bbox = [0.0; 4];
        for (i, v) in bbox.iter_mut().enumerate() {
            *v = number(&box_obj.get_item(i).ok()?)?;
        }
        records.push(pack_line(&line, bbox, source, &mut fonts)?);
    }
    Some(records)
}

/// 在已核验的普通槽对象上读取数值，直接复用所有入口的尺度与字体语义。
fn pack_line(
    line: &Bound<'_, PyAny>,
    bbox: [f64; 4],
    source: usize,
    fonts: &mut HashMap<(String, i64), usize>,
) -> Option<docvortex_core::rule_text::TailLine> {
    let em = number(&line.getattr(pyo3::intern!(line.py(), "em_height")).ok()?)?;
    let height = number(
        &line
            .getattr(pyo3::intern!(line.py(), "effective_height"))
            .ok()?,
    )?;
    let repaired = line
        .getattr(pyo3::intern!(line.py(), "style_scale_repaired"))
        .ok()?;
    let restored = line
        .getattr(pyo3::intern!(line.py(), "restored_inline_cluster"))
        .ok()?;
    if !repaired.is_exact_instance_of::<PyBool>() || !restored.is_exact_instance_of::<PyBool>() {
        return None;
    }
    let scale = if repaired.extract::<bool>().ok()? && em > 0.0 {
        em
    } else if height != 0.0 {
        height
    } else {
        bbox[3] - bbox[1]
    };
    let signature = line
        .getattr(pyo3::intern!(line.py(), "font_signature"))
        .ok()?;
    let font = if signature.is_none() {
        None
    } else {
        if !signature.is_exact_instance_of::<PyTuple>() || signature.len().ok()? != 2 {
            return None;
        }
        let name = signature.get_item(0).ok()?;
        let flags = signature.get_item(1).ok()?;
        if !name.is_exact_instance_of::<PyString>() || !flags.is_exact_instance_of::<PyInt>() {
            return None;
        }
        let key = (name.extract::<String>().ok()?, flags.extract::<i64>().ok()?);
        let next = fonts.len();
        Some(*fonts.entry(key).or_insert(next))
    };
    let coverage = number(
        &line
            .getattr(pyo3::intern!(line.py(), "font_coverage"))
            .ok()?,
    )?;
    let weight = optional_number(
        &line
            .getattr(pyo3::intern!(line.py(), "dominant_font_weight"))
            .ok()?,
    )?;
    let baseline = optional_number(&line.getattr(pyo3::intern!(line.py(), "baseline")).ok()?)?;
    let visual = line
        .getattr(pyo3::intern!(line.py(), "visual_row_id"))
        .ok()?;
    let visual = if visual.is_none() {
        None
    } else {
        if !visual.is_exact_instance_of::<PyInt>() {
            return None;
        }
        Some(visual.extract::<i64>().ok()?)
    };
    let index = line
        .getattr(pyo3::intern!(line.py(), "source_index"))
        .ok()?
        .extract::<i64>()
        .ok()?;
    Some((
        bbox,
        scale.max(0.1),
        font,
        coverage,
        weight,
        baseline,
        restored.extract::<bool>().ok()?,
        visual,
        index,
        source,
    ))
}

/// 整页栏带只读一次，验证全部成员后返回原对象；不物化 Python 待处理列表或重复排序。
#[pyfunction]
pub(super) fn short_tail_lane_events(
    py: Python<'_>,
    lanes: &Bound<'_, PyAny>,
    median: &Bound<'_, PyAny>,
    lane_type: &Bound<'_, PyAny>,
    line_type: &Bound<'_, PyAny>,
) -> Option<Vec<(usize, Py<PyAny>, usize)>> {
    if !lanes.is_exact_instance_of::<PyList>() {
        return None;
    }
    let median = number(median)?;
    let mut bounds = Vec::new();
    let mut records = Vec::new();
    let mut references = Vec::new();
    let mut seen_lines = HashSet::new();
    let mut seen_indices = HashSet::new();
    let mut fonts = HashMap::new();
    for (source, lane) in lanes.try_iter().ok()?.enumerate() {
        let lane = lane.ok()?;
        if !lane.get_type().is(lane_type) {
            return None;
        }
        bounds.push((
            number(&lane.getattr(pyo3::intern!(lane.py(), "left")).ok()?)?,
            number(&lane.getattr(pyo3::intern!(lane.py(), "right")).ok()?)?,
        ));
        let rows = lane.getattr(pyo3::intern!(lane.py(), "lines")).ok()?;
        if !rows.is_exact_instance_of::<PyList>() {
            return None;
        }
        for row in rows.try_iter().ok()? {
            let row = row.ok()?;
            if !row.is_exact_instance_of::<PyTuple>() || row.len().ok()? != 2 {
                return None;
            }
            let line = row.get_item(0).ok()?;
            let bbox_obj = row.get_item(1).ok()?;
            if !line.get_type().is(line_type)
                || (!bbox_obj.is_exact_instance_of::<PyTuple>()
                    && !bbox_obj.is_exact_instance_of::<PyList>())
                || bbox_obj.len().ok()? != 4
            {
                return None;
            }
            let index_obj = line
                .getattr(pyo3::intern!(line.py(), "source_index"))
                .ok()?;
            if !index_obj.is_exact_instance_of::<PyInt>() {
                return None;
            }
            let index = index_obj.extract::<i64>().ok()?;
            if !seen_lines.insert(line.as_ptr() as usize) || !seen_indices.insert(index) {
                return None;
            }
            let mut bbox = [0.0; 4];
            for (i, value) in bbox.iter_mut().enumerate() {
                *value = number(&bbox_obj.get_item(i).ok()?)?;
            }
            let semantic = line
                .getattr(pyo3::intern!(line.py(), "semantic_type"))
                .ok()?;
            if !semantic.is_none() {
                if !semantic.is_exact_instance_of::<PyString>() {
                    return None;
                }
                continue;
            }
            records.push(pack_line(&line, bbox, source, &mut fonts)?);
            references.push((source, row.unbind()));
        }
    }
    let moves =
        py.detach(|| docvortex_core::rule_text::short_tail_destinations(&records, &bounds, median));
    Some(
        moves
            .into_iter()
            .map(|(i, target)| (references[i].0, references[i].1.clone_ref(py), target))
            .collect(),
    )
}

/// 数据读取保留 GIL；完成普通数值快照后才释放 GIL 计算完整短尾事件。
#[pyfunction]
pub(super) fn short_tail_destinations_owned(
    py: Python<'_>,
    pending: &Bound<'_, PyAny>,
    lanes: Vec<(f64, f64)>,
    median: f64,
) -> Option<Vec<(usize, usize)>> {
    if !median.is_finite()
        || median.abs() > 2f64.powi(50)
        || lanes
            .iter()
            .any(|&(a, b)| !a.is_finite() || !b.is_finite() || a.abs().max(b.abs()) > 2f64.powi(50))
    {
        return None;
    }
    let records = pack_lines(pending)?;
    if records.iter().any(|r| r.9 >= lanes.len()) {
        return None;
    }
    Some(py.detach(|| docvortex_core::rule_text::short_tail_destinations(&records, &lanes, median)))
}

/// 当前普通行只读一次；源成员和语义类型不在调用间缓存。
fn lane_geometry(
    rows: &Bound<'_, PyAny>,
    line_type: &Bound<'_, PyAny>,
    need_scale: bool,
    need_semantic: bool,
) -> Option<Vec<docvortex_core::rule_text::LaneGeometry>> {
    if !rows.is_exact_instance_of::<PyList>() {
        return None;
    }
    let mut output = Vec::new();
    for row in rows.try_iter().ok()? {
        let row = row.ok()?;
        if !row.is_exact_instance_of::<PyTuple>() || row.len().ok()? != 2 {
            return None;
        }
        let line = row.get_item(0).ok()?;
        let box_obj = row.get_item(1).ok()?;
        if !line.get_type().is(line_type)
            || (!box_obj.is_exact_instance_of::<PyTuple>()
                && !box_obj.is_exact_instance_of::<PyList>())
            || box_obj.len().ok()? != 4
        {
            return None;
        }
        let mut bbox = [0.0; 4];
        for (i, value) in bbox.iter_mut().enumerate() {
            *value = number(&box_obj.get_item(i).ok()?)?;
        }
        // 常规成员分栏只消费框；嵌套栏才读取语义，锚点推断另外读取字号。
        let semantic = if need_semantic {
            line.getattr(pyo3::intern!(line.py(), "semantic_type"))
                .ok()?
        } else {
            line.py().None().into_bound(line.py())
        };
        let semantic = if semantic.is_none() {
            String::new()
        } else {
            if !semantic.is_exact_instance_of::<PyString>() {
                return None;
            }
            semantic.extract::<String>().ok()?
        };
        let scale = if need_scale {
            let repaired = line
                .getattr(pyo3::intern!(line.py(), "style_scale_repaired"))
                .ok()?;
            if !repaired.is_exact_instance_of::<PyBool>() {
                return None;
            }
            let em = number(&line.getattr(pyo3::intern!(line.py(), "em_height")).ok()?)?;
            let height = number(
                &line
                    .getattr(pyo3::intern!(line.py(), "effective_height"))
                    .ok()?,
            )?;
            if repaired.extract::<bool>().ok()? && em > 0.0 {
                em
            } else if height != 0.0 {
                height
            } else {
                bbox[3] - bbox[1]
            }
        } else {
            0.1
        };
        let excluded = matches!(
            semantic.as_str(),
            "header" | "footer" | "page_number" | "aside_text"
        );
        output.push((
            bbox,
            scale.max(0.1),
            !excluded,
            excluded || semantic == "page_footnote",
        ));
    }
    Some(output)
}

/// 锚点聚类与区间筛选使用纯数值快照，返回完整输入中的锚点索引。
#[pyfunction]
pub(super) fn supported_lane_intervals_owned(
    py: Python<'_>,
    rows: &Bound<'_, PyAny>,
    width: &Bound<'_, PyAny>,
    median: &Bound<'_, PyAny>,
    line_type: &Bound<'_, PyAny>,
) -> Option<(Vec<(f64, f64, usize)>, Vec<usize>)> {
    let lines = lane_geometry(rows, line_type, true, true)?;
    if lines.is_empty() {
        return None;
    }
    let width = number(width)?;
    let median = number(median)?;
    let anchors = lines
        .iter()
        .enumerate()
        .filter(|(_, line)| line.2)
        .map(|(i, _)| i)
        .collect();
    Some((
        py.detach(|| docvortex_core::rule_text::supported_lane_intervals(&lines, width, median)),
        anchors,
    ))
}

/// 分栏判断批量读取新鲜字段，释放 GIL 以后只处理自有数值，原对象由 Python 继续持有。
#[pyfunction]
pub(super) fn lane_assignments_owned(
    py: Python<'_>,
    rows: &Bound<'_, PyAny>,
    lanes: &Bound<'_, PyAny>,
    tolerance: &Bound<'_, PyAny>,
    nested: Option<(f64, f64)>,
    line_type: &Bound<'_, PyAny>,
    lane_type: &Bound<'_, PyAny>,
) -> Option<Vec<Option<usize>>> {
    let lines = lane_geometry(rows, line_type, false, nested.is_some())?;
    let tolerance = number(tolerance)?;
    if !lanes.is_exact_instance_of::<PyList>()
        || lanes.len().ok()? == 0
        || nested.is_some_and(|(a, b)| !a.is_finite() || !b.is_finite())
    {
        return None;
    }
    let mut bounds = Vec::new();
    for lane in lanes.try_iter().ok()? {
        let lane = lane.ok()?;
        if !lane.get_type().is(lane_type) {
            return None;
        }
        bounds.push((
            number(&lane.getattr(pyo3::intern!(lane.py(), "left")).ok()?)?,
            number(&lane.getattr(pyo3::intern!(lane.py(), "right")).ok()?)?,
        ));
    }
    Some(
        py.detach(|| {
            docvortex_core::rule_text::lane_assignments(&lines, &bounds, tolerance, nested)
        }),
    )
}
