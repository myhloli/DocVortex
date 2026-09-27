//! 只在唯一 Unicode/字体值和最终样式行边界访问 Python，字符几何直接来自快照。
use docvortex_core::{
    geometry::Box4,
    inline_styles::{self, Character, Drawing, Line, Payload},
    text_snapshot::TextSnapshot,
};
use pyo3::{exceptions::PyValueError, prelude::*};
use std::{
    collections::HashMap,
    sync::atomic::{AtomicU64, Ordering},
};
static CALLS: AtomicU64 = AtomicU64::new(0);

/// 读取解释器定义的 Unicode 与字体样式，按批次索引执行完整样式阶段。
pub(super) fn detect(
    py: Python<'_>,
    data: &TextSnapshot,
    rows: Vec<(Box4, i64, Vec<usize>)>,
    drawings: Vec<(Box4, f64)>,
    text_properties: &Bound<'_, PyAny>,
    font_bold: &Bound<'_, PyAny>,
    thresholds: [f64; 8],
    min_bold: usize,
) -> PyResult<Option<Vec<Payload>>> {
    if rows
        .iter()
        .flat_map(|r| &r.2)
        .any(|&i| i >= data.chars.len())
    {
        return Err(PyValueError::new_err("invalid style member index"));
    }
    if thresholds.iter().any(|v| !v.is_finite() || *v < 0.0) {
        return Err(PyValueError::new_err("invalid style thresholds"));
    }
    if rows
        .iter()
        .flat_map(|r| &r.0)
        .chain(
            drawings
                .iter()
                .flat_map(|r| r.0.iter().chain(std::iter::once(&r.1))),
        )
        .chain(
            data.chars
                .iter()
                .flat_map(|c| c.bbox.iter().chain(c.tight.iter().flatten())),
        )
        .any(|v| !v.is_finite() || v.abs() > 1e100)
    {
        return Ok(None);
    }
    let mut properties: HashMap<&str, (String, bool, bool, bool)> = HashMap::new();
    let mut fonts = HashMap::new();
    let mut chars = Vec::with_capacity(data.chars.len());
    for c in &data.chars {
        let p = if let Some(p) = properties.get(c.text.as_str()) {
            p
        } else {
            let value = text_properties.call1((&c.text,))?.extract()?;
            properties.entry(c.text.as_str()).or_insert(value)
        };
        let f = &data.fonts[c.font];
        let key = (f.name.as_str(), f.flags, f.weight);
        let bold = if let Some(&b) = fonts.get(&key) {
            b
        } else {
            let b = font_bold.call1((&f.name, f.flags, f.weight))?.extract()?;
            fonts.insert(key, b);
            b
        };
        chars.push(Character {
            bbox: c.bbox,
            tight: if data.extended { c.tight } else { None },
            index: c.index,
            fragment: p.0.clone(),
            visible: p.1,
            space: p.2,
            marker: p.3,
            bold,
        });
    }
    let lines = rows
        .into_iter()
        .map(|(bbox, source, members)| Line {
            bbox,
            source,
            members,
        })
        .collect();
    let drawings: Vec<_> = drawings
        .into_iter()
        .map(|(bbox, width)| Drawing { bbox, width })
        .collect();
    let output =
        py.detach(|| inline_styles::detect(&chars, lines, &drawings, thresholds, min_bold));
    if output.is_some() {
        CALLS.fetch_add(1, Ordering::Relaxed);
    }
    Ok(output)
}

/// 报告真正完成的样式批次数，避免用配置代替原生路径命中证据。
#[pyfunction]
pub fn inline_style_stats() -> u64 {
    CALLS.load(Ordering::Relaxed)
}
