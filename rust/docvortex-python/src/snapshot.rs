//! 同库原始读取直接进入自有 canonical 快照，只在兼容边界构造 Python 字符。
use docvortex_core::geometry_risk::{Entry, RunKey};
use docvortex_core::{
    extraction,
    geometry::{self, SourceRow},
    text_assignment, text_content,
    text_pipeline::{self, TextChar},
    text_snapshot::{
        self as snapshot, Angle, Character, Font, InputCharacter, Properties, SnapshotError,
        TextSnapshot,
    },
};
use docvortex_pdfium::{ReadError, Record};
use pyo3::{
    exceptions::{PyKeyError, PyMemoryError, PyValueError},
    prelude::*,
    types::{PyBytes, PyDict, PyList, PyTuple},
};
use std::{
    collections::{HashMap, HashSet},
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
};

static SPAN_CONTENT_CALLS: AtomicU64 = AtomicU64::new(0);
static SPAN_ASSIGNMENT_CALLS: AtomicU64 = AtomicU64::new(0);
static SPAN_ASSIGNMENT_UNSUPPORTED: AtomicU64 = AtomicU64::new(0);

/// 公开实际原生字符归属调用次数及显式不支持选择，避免把接口存在视为已执行。
#[pyfunction]
pub fn text_snapshot_stats() -> (u64, u64, u64) {
    (
        SPAN_ASSIGNMENT_CALLS.load(Ordering::Relaxed),
        SPAN_ASSIGNMENT_UNSUPPORTED.load(Ordering::Relaxed),
        SPAN_CONTENT_CALLS.load(Ordering::Relaxed),
    )
}

/// 保留读取错误类型，不把计算错误改写为参考路径选择。
fn read_error(error: ReadError) -> PyErr {
    match error {
        ReadError::InvalidInput(message) => PyValueError::new_err(message),
        ReadError::Pdfium(message) => super::pdfium::PdfiumReadError::new_err(message),
        ReadError::Allocation(message) => PyMemoryError::new_err(message),
    }
}
/// 保留缺失代理来源的 KeyError，其他已准入算法失败明确传播。
fn snapshot_error(error: SnapshotError) -> PyErr {
    match error {
        SnapshotError::MissingSource(index) => PyKeyError::new_err(index),
        SnapshotError::Invalid(message) => PyValueError::new_err(message),
    }
}
/// 正负零属于相同字体值，但首个字体记录仍保存原始位型。
fn number_key(value: f64) -> u64 {
    if value == 0.0 {
        0
    } else {
        value.to_bits()
    }
}
/// 按不同字符串读取宿主 Unicode 语义，不为每个字符构造 Python 字典。
fn prepare_properties(
    py: Python<'_>,
    values: impl IntoIterator<Item = String>,
    output: &mut HashMap<String, Properties>,
) -> PyResult<()> {
    let han = py
        .import("docvortex.document.pdf.text.dedup")?
        .getattr("_is_han")?;
    for value in values {
        if output.contains_key(&value) {
            continue;
        }
        let text = pyo3::types::PyString::new(py, &value);
        let properties = Properties {
            space: text.call_method0("isspace")?.extract()?,
            canonical: value.clone(),
            han: han.call1((&text,))?.extract()?,
        };
        output.insert(value, properties);
    }
    Ok(())
}

/// 只为实际映射组准备 canonical 值，保留普通页面不读取部首资源的惰性行为。
fn prepare_mapping_properties(
    py: Python<'_>,
    chars: &[Character],
    output: &mut HashMap<String, Properties>,
) -> PyResult<()> {
    let requested = snapshot::mapping_metadata(chars, output);
    if requested.is_empty() {
        return Ok(());
    }
    let canonical = py
        .import("docvortex.document.pdf.text.dedup")?
        .getattr("_canonical_han")?;
    for text in requested {
        let value: String = canonical.call1((&text,))?.extract()?;
        prepare_properties(py, std::iter::once(value.clone()), output)?;
        output
            .get_mut(&text)
            .expect("raw text properties prepared")
            .canonical = value;
    }
    Ok(())
}

/// 按不同书写角保留 CPython round 的两种量化和 math.sin/cos 的平台结果。
fn prepare_angles(py: Python<'_>, glyphs: &[snapshot::Glyph]) -> PyResult<HashMap<u64, Angle>> {
    let math = py.import("math")?;
    let round = py.import("builtins")?.getattr("round")?;
    let mut output = HashMap::new();
    for glyph in glyphs {
        let key = glyph.angle.to_bits();
        if output.contains_key(&key) {
            continue;
        }
        let value = glyph.angle;
        let rounded: f64 = round.call1((value, 3))?.extract()?;
        output.insert(
            key,
            Angle {
                cos: math.getattr("cos")?.call1((value,))?.extract()?,
                sin: math.getattr("sin")?.call1((value,))?.extract()?,
                rounded: number_key(rounded),
                bucket: round.call1((value * 1000.0,))?.extract()?,
            },
        );
    }
    Ok(output)
}
/// 普通 PDF 数值准入在任何 canonical 算法前完成；异常量级必须明确选择参考实现。
fn supported(records: &[Record], frame: [f64; 4]) -> bool {
    let ordinary = |value: f64| value.is_finite() && value.abs() <= 1e12;
    frame.into_iter().all(ordinary)
        && records.iter().all(|record| {
            ordinary(record.1)
                && ordinary(record.5)
                && record
                    .2
                    .into_iter()
                    .flat_map(|b| [b.0, b.1, b.2, b.3])
                    .all(ordinary)
                && record
                    .3
                    .into_iter()
                    .flat_map(|b| [b.0, b.1, b.2, b.3])
                    .all(ordinary)
                && record.9.into_iter().flat_map(|p| [p.0, p.1]).all(ordinary)
        })
}

#[pyclass(frozen, weakref, module = "docvortex._native")]
pub struct NativeTextSnapshot {
    data: Arc<TextSnapshot>,
    raw_count: usize,
}

struct GeometryRecord {
    source: SourceRow,
    family: String,
    font_size: f64,
    rounded_font_size: f64,
    flags: i32,
    weight: i32,
    script: String,
    anchor: bool,
}

#[pyclass(frozen, module = "docvortex._native")]
pub struct NativeGeometryEvidence {
    records: Vec<GeometryRecord>,
}

static GEOMETRY_EVIDENCE_PREPARES: AtomicU64 = AtomicU64::new(0);
static GEOMETRY_EVIDENCE_LINES: AtomicU64 = AtomicU64::new(0);
static GEOMETRY_EVIDENCE_FALLBACKS: AtomicU64 = AtomicU64::new(0);

/// Python float 字典语义把正负零视为同键；普通有限值按位型保持区分。
fn geometry_number_key(value: f64) -> u64 {
    if value == 0.0 {
        0
    } else {
        value.to_bits()
    }
}

/// 报告页面级 geometry evidence 准备、风险行命中和明确回退次数。
#[pyfunction]
pub fn geometry_evidence_stats() -> (u64, u64, u64) {
    (
        GEOMETRY_EVIDENCE_PREPARES.load(Ordering::Relaxed),
        GEOMETRY_EVIDENCE_LINES.load(Ordering::Relaxed),
        GEOMETRY_EVIDENCE_FALLBACKS.load(Ordering::Relaxed),
    )
}

/// 同步借用 PDFium 读取原始字符；函数和句柄由已核验 ABI 的宿主 guard 保活。
#[pyfunction]
pub fn read_pdfium_text_snapshot(
    py: Python<'_>,
    addresses: Vec<usize>,
    color_addresses: [usize; 2],
    handle: usize,
    count: usize,
    extended: bool,
    frame: [f64; 4],
    rotation: i32,
    visibility: Option<HashMap<usize, (bool, Option<[f64; 4]>)>>,
) -> PyResult<Option<NativeTextSnapshot>> {
    if color_addresses.contains(&0) || ![0, 90, 180, 270].contains(&rotation) {
        return Err(PyValueError::new_err("invalid snapshot ABI or rotation"));
    }
    let (records, raw_fonts) =
        unsafe { docvortex_pdfium::read_characters(addresses, handle, count, extended) }
            .map_err(read_error)?;
    if !supported(&records, frame)
        || visibility.as_ref().is_some_and(|items| {
            items.values().any(|(_, clip)| {
                clip.is_some_and(|b| b.iter().any(|v| !v.is_finite() || v.abs() > 1e12))
            })
        })
    {
        return Ok(None);
    }
    let mut properties = HashMap::new();
    let mut scalars: HashSet<String> = records
        .iter()
        .map(|r| char::from_u32(r.0).unwrap_or('\u{fffd}').to_string())
        .collect();
    for pair in records.windows(2) {
        if (0xd800..=0xdbff).contains(&pair[0].0) && (0xdc00..=0xdfff).contains(&pair[1].0) {
            scalars.insert(
                char::from_u32(0x10000 + ((pair[0].0 - 0xd800) << 10) + pair[1].0 - 0xdc00)
                    .unwrap()
                    .to_string(),
            );
        }
    }
    prepare_properties(py, scalars, &mut properties)?;
    let decoded: Vec<(String, i32)> = raw_fonts
        .into_iter()
        .map(|(name, flags)| {
            PyBytes::new(py, &name)
                .call_method1("decode", ("utf-8", "replace"))?
                .extract::<String>()
                .map(|name| (name, flags))
        })
        .collect::<PyResult<_>>()?;
    let geometry = extraction::materialize(
        records
            .iter()
            .map(|r| {
                let selected = if r.1 == 0.0 { r.2 } else { r.3 };
                (
                    selected.expect("reader validated selected box").into(),
                    if extended { r.2.map(Into::into) } else { None },
                    if extended { r.3.map(Into::into) } else { None },
                    r.9.map(Into::into),
                )
            })
            .collect(),
        frame,
        [
            (frame[2] - frame[0]).abs().ceil(),
            (frame[3] - frame[1]).abs().ceil(),
        ],
        rotation,
    );
    let mut fonts = Vec::new();
    let mut font_ids = HashMap::new();
    let mut objects = HashMap::new();
    let mut input = Vec::with_capacity(records.len());
    for (index, (r, (bbox, loose, tight, origin))) in records.into_iter().zip(geometry).enumerate()
    {
        let (name, flags) = &decoded[r.4];
        let key = (name.clone(), *flags, number_key(r.5), r.6);
        let font = *font_ids.entry(key).or_insert_with(|| {
            let id = fonts.len();
            fonts.push(Font {
                name: name.clone(),
                name_id: r.4,
                flags: *flags,
                size: r.5,
                weight: r.6,
            });
            id
        });
        let object = if r.7 == 0 {
            None
        } else {
            let next = objects.len();
            Some(*objects.entry(r.7).or_insert(next))
        };
        let (admitted, clip) = visibility
            .as_ref()
            .and_then(|v| v.get(&r.7))
            .copied()
            .unwrap_or((true, None));
        input.push(InputCharacter {
            admitted,
            clip,
            character: Character {
                text: char::from_u32(r.0).unwrap_or('\u{fffd}').to_string(),
                bbox,
                rotation: r.1,
                font,
                index,
                sources: vec![index],
                code: r.0,
                object,
                mode: r.8,
                writing_angle: (rotation as f64) * (std::f64::consts::PI / 180.0) - r.1,
                origin,
                loose,
                tight,
                visible: None,
            },
        });
    }
    let mut chars = snapshot::initialize(input, &properties);
    let mut runs = snapshot::writing_runs(&chars);
    let mut directions = HashMap::new();
    let math = py.import("math")?;
    let hypot = math.getattr("hypot")?;
    let atan2 = math.getattr("atan2")?;
    loop {
        let requested = snapshot::advance_writing_angles(&mut chars, &mut runs, &directions);
        if requested.is_empty() {
            break;
        }
        for (dx, dy) in requested {
            let (x, y) = (f64::from_bits(dx), f64::from_bits(dy));
            let valid = hypot.call1((x, y))?.extract::<f64>()? > 0.001;
            let angle = if valid {
                atan2.call1((y, x))?.extract()?
            } else {
                0.0
            };
            directions.insert((dx, dy), (valid, angle));
        }
    }
    if chars.iter().any(|ch| ch.mode == Some(3)) {
        let mut objects = HashMap::new();
        let mut queries = Vec::new();
        for ch in &chars {
            if let Some(object) = ch.object {
                objects.entry(object).or_insert_with(|| {
                    let id = queries.len();
                    queries.push((ch.index, ch.mode));
                    id
                });
            }
        }
        let visible = unsafe {
            docvortex_pdfium::text_colors::read_visible(color_addresses, handle, &queries)
        }
        .map_err(read_error)?;
        for ch in &mut chars {
            ch.visible = Some(ch.object.is_some_and(|id| visible[objects[&id]]));
        }
    }
    let chars = snapshot::restore_surrogates(chars, count).map_err(snapshot_error)?;
    prepare_mapping_properties(py, &chars, &mut properties)?;
    let glyphs = snapshot::mapping_groups(chars, &properties);
    let angles = prepare_angles(py, &glyphs)?;
    let glyphs = py
        .detach(|| snapshot::collapse_paints(glyphs, &angles, &properties))
        .map_err(snapshot_error)?;
    let pairs = py
        .detach(|| snapshot::hidden_pairs(&glyphs, &angles, &properties))
        .map_err(snapshot_error)?;
    let mut normalized = HashMap::new();
    if !pairs.is_empty() {
        let comparison = py
            .import("docvortex.document.pdf.text.dedup")?
            .getattr("_comparison_text")?;
        for (a, b) in &pairs {
            for indices in [a, b] {
                let text: String = indices.iter().map(|&i| glyphs[i].text.as_str()).collect();
                if let std::collections::hash_map::Entry::Vacant(entry) = normalized.entry(text) {
                    let value: String = comparison.call1((entry.key(),))?.extract()?;
                    prepare_properties(py, value.chars().map(|c| c.to_string()), &mut properties)?;
                    entry.insert(value);
                }
            }
        }
    }
    let chars = py.detach(|| snapshot::suppress_hidden(glyphs, pairs, &normalized, &properties));
    Ok(Some(NativeTextSnapshot {
        raw_count: count,
        data: Arc::new(TextSnapshot {
            chars,
            fonts,
            extended,
            visible_only: visibility.is_some(),
        }),
    }))
}

impl NativeTextSnapshot {
    /// 一次准备页面级风险输入；解释器 Unicode/round 语义只在不同文本和字体上调用。
    fn prepare_geometry_evidence_impl(
        &self,
        py: Python<'_>,
    ) -> PyResult<Option<NativeGeometryEvidence>> {
        let char_geometry = py.import("docvortex.analyzers.native.pdf.char_geometry")?;
        let script_group = char_geometry.getattr("_script_group")?;
        let is_anchor_text = char_geometry.getattr("_is_anchor_text")?;
        let normalize = char_geometry.getattr("_normalized_font_family")?;
        let round = py.import("builtins")?.getattr("round")?;
        let mut groups: HashMap<String, (String, bool)> = HashMap::new();
        let mut families: HashMap<String, String> = HashMap::new();
        let mut font_metadata: HashMap<usize, (String, f64, f64, i32, i32)> = HashMap::new();
        let mut records = Vec::with_capacity(self.data.chars.len());
        for ch in &self.data.chars {
            let font = &self.data.fonts[ch.font];
            if !font.size.is_finite() {
                GEOMETRY_EVIDENCE_FALLBACKS.fetch_add(1, Ordering::Relaxed);
                return Ok(None);
            }
            let metadata = if let Some(value) = font_metadata.get(&ch.font) {
                value.clone()
            } else {
                let raw_name = if font.name.is_empty() {
                    "<unknown>".to_owned()
                } else {
                    font.name.clone()
                };
                let family = if let Some(value) = families.get(&raw_name) {
                    value.clone()
                } else {
                    let value: String = normalize.call1((&raw_name,))?.extract()?;
                    families.insert(raw_name.clone(), value.clone());
                    value
                };
                let rounded_size: f64 =
                    round.call1((font.size * 4.0,))?.extract::<i64>()? as f64 / 4.0;
                let weight: i64 = round.call1((f64::from(font.weight) / 100.0,))?.extract()?;
                // 分组使用取整字号，风险阈值仍须消费 PDFium 的原始字号。
                let value = (family, font.size, rounded_size, font.flags, weight as i32);
                font_metadata.insert(ch.font, value.clone());
                value
            };
            let (script, anchor) = if let Some(value) = groups.get(&ch.text) {
                value.clone()
            } else {
                let group: String = script_group.call1((&ch.text,))?.extract()?;
                let anchor: bool = is_anchor_text.call1((&ch.text,))?.extract()?;
                let value = (group.clone(), anchor);
                groups.insert(ch.text.clone(), value.clone());
                value
            };
            records.push(GeometryRecord {
                source: (Some(ch.bbox), ch.loose, ch.tight, ch.origin, ch.rotation),
                family: metadata.0,
                font_size: metadata.1,
                rounded_font_size: metadata.2,
                flags: metadata.3,
                weight: metadata.4,
                script,
                anchor,
            });
        }
        GEOMETRY_EVIDENCE_PREPARES.fetch_add(1, Ordering::Relaxed);
        Ok(Some(NativeGeometryEvidence { records }))
    }

    /// 一次物化中的字体字典共享，字符和 Bbox 独立；不会在 Rust 快照缓存 Python 可变对象。
    fn geometry<'py>(&self, py: Python<'py>) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyList>)> {
        let bbox_type = py
            .import("docvortex.document.pdf.text._contracts")?
            .getattr("Bbox")?;
        let chars = PyList::empty(py);
        // 一次缓存字段名，避免每个字符重复走解释器 intern 查找。
        let key_bbox = pyo3::intern!(py, "bbox");
        let key_char = pyo3::intern!(py, "char");
        let key_rotation = pyo3::intern!(py, "rotation");
        let key_font = pyo3::intern!(py, "font");
        let key_char_idx = pyo3::intern!(py, "char_idx");
        let key_source_indices = pyo3::intern!(py, "source_indices");
        let key_raw_code = pyo3::intern!(py, "raw_code");
        let key_text_object_id = pyo3::intern!(py, "text_object_id");
        let key_text_render_mode = pyo3::intern!(py, "text_render_mode");
        let key_writing_angle = pyo3::intern!(py, "writing_angle");
        let key_origin = pyo3::intern!(py, "origin");
        let key_name = pyo3::intern!(py, "name");
        let key_flags = pyo3::intern!(py, "flags");
        let key_size = pyo3::intern!(py, "size");
        let key_weight = pyo3::intern!(py, "weight");
        let key_loose_bbox = pyo3::intern!(py, "loose_bbox");
        let key_tight_bbox = pyo3::intern!(py, "tight_bbox");
        let key_text_is_visible = pyo3::intern!(py, "text_is_visible");
        let tight = PyDict::new(py);
        let loose = PyDict::new(py);
        let origins = PyDict::new(py);

        // 字体表由 Rust 索引直接寻址，避免每个字符再做一次哈希查找。
        let mut fonts: Vec<Option<Bound<'py, PyDict>>> = vec![None; self.data.fonts.len()];
        let mut names: HashMap<usize, Bound<'py, pyo3::types::PyString>> = HashMap::new();
        for (font_id, slot) in fonts.iter_mut().enumerate() {
            let font = &self.data.fonts[font_id];
            let value = PyDict::new(py);
            let name = names
                .entry(font.name_id)
                .or_insert_with(|| pyo3::types::PyString::new(py, &font.name));
            value.set_item(key_name, &*name)?;
            value.set_item(key_flags, font.flags)?;
            value.set_item(key_size, font.size)?;
            value.set_item(key_weight, font.weight)?;
            *slot = Some(value);
        }

        for ch in &self.data.chars {
            let value = PyDict::new(py);
            let bbox = bbox_type.call1((PyList::new(py, ch.bbox)?,))?;
            let text = pyo3::types::PyString::new(py, &ch.text);
            // 先转换一次不可变坐标容器；字符字段和 loose/tight/origin 侧表必须共享同一对象。
            let origin = ch
                .origin
                .map(|p| (p[0], p[1]))
                .into_pyobject(py)?
                .into_any()
                .unbind();
            let loose_bbox = ch
                .loose
                .map(|b| (b[0], b[1], b[2], b[3]))
                .into_pyobject(py)?
                .into_any()
                .unbind();
            let tight_bbox = ch
                .tight
                .map(|b| (b[0], b[1], b[2], b[3]))
                .into_pyobject(py)?
                .into_any()
                .unbind();
            value.set_item(key_bbox, bbox)?;
            value.set_item(key_char, text)?;
            value.set_item(key_rotation, ch.rotation)?;
            value.set_item(
                key_font,
                fonts[ch.font].as_ref().expect("font materialized"),
            )?;
            value.set_item(key_char_idx, ch.index)?;
            value.set_item(
                key_source_indices,
                PyTuple::new(py, ch.sources.iter().copied())?,
            )?;
            value.set_item(key_raw_code, ch.code)?;
            value.set_item(key_text_object_id, ch.object)?;
            value.set_item(key_text_render_mode, ch.mode)?;
            value.set_item(key_writing_angle, ch.writing_angle)?;
            value.set_item(key_origin, &origin)?;
            if self.data.extended {
                value.set_item(key_loose_bbox, &loose_bbox)?;
                value.set_item(key_tight_bbox, &tight_bbox)?;
                if ch.loose.is_some() && ch.rotation.abs() > 1e-9 {
                    loose.set_item(ch.index, loose_bbox.clone_ref(py))?;
                }
                if ch.tight.is_some() {
                    tight.set_item(ch.index, tight_bbox.clone_ref(py))?;
                }
                if ch.origin.is_some() {
                    origins.set_item(ch.index, origin.clone_ref(py))?;
                }
            }
            if let Some(visible) = ch.visible {
                value.set_item(key_text_is_visible, visible)?;
            }
            chars.append(value)?;
        }
        let geometry = py
            .import("docvortex.document.pdf.native_contracts")?
            .getattr("PDFPageTextGeometry")?
            .call1((&chars, tight, origins, loose))?;
        Ok((geometry, chars))
    }
}

#[pymethods]
impl NativeTextSnapshot {
    /// 一次准备全文风险使用的页面级 geometry evidence。
    fn prepare_geometry_evidence(
        &self,
        py: Python<'_>,
    ) -> PyResult<Option<NativeGeometryEvidence>> {
        self.prepare_geometry_evidence_impl(py)
    }

    /// 兼容属性每次创建独立输出，快照在页面关闭后仍能使用。
    fn materialize_geometry<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.geometry(py).map(|(geometry, _)| geometry)
    }
    /// 不可变 Rust 数据可安全共享给 deepcopy，绝不缓存兼容 Python 字典。
    fn __deepcopy__(&self, _memo: &Bound<'_, PyAny>) -> Self {
        Self {
            data: self.data.clone(),
            raw_count: self.raw_count,
        }
    }
    /// 自有数据直接生成基础组行结果，供表格方向等早期消费者使用。
    #[pyo3(signature=(height_threshold=0.7, distance=0.1))]
    fn prepare_grouped_evidence<'py>(
        &self,
        py: Python<'py>,
        height_threshold: f64,
        distance: f64,
    ) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyList>)> {
        let records: Vec<TextChar> = self
            .data
            .chars
            .iter()
            .map(|ch| TextChar {
                bbox: ch.bbox,
                text: ch.text.clone(),
                rotation: ch.rotation,
                font_id: ch.font,
                font_size: Some(self.data.fonts[ch.font].size),
                signature_id: None,
                font_weight: None,
            })
            .collect();
        let unicode = super::text_pipeline::unicode_properties(py, &records)?;
        let lines = py.detach(|| {
            text_pipeline::group_text_lines(&records, height_threshold, distance, &unicode)
        });
        let (geometry, chars) = self.geometry(py)?;
        let lines = super::text_pipeline::materialize_grouped_lines(py, &chars, lines)?;
        Ok((geometry, lines))
    }
    /// 方向判断只物化粗行的框、旋转和文本，不创建任何 Python Char、字体或 Bbox 对象。
    #[pyo3(signature=(height_threshold=0.7, distance=0.1))]
    fn get_line_summaries<'py>(
        &self,
        py: Python<'py>,
        height_threshold: f64,
        distance: f64,
    ) -> PyResult<Bound<'py, PyList>> {
        let records: Vec<TextChar> = self
            .data
            .chars
            .iter()
            .map(|ch| TextChar {
                bbox: ch.bbox,
                text: ch.text.clone(),
                rotation: ch.rotation,
                font_id: ch.font,
                font_size: Some(self.data.fonts[ch.font].size),
                signature_id: None,
                font_weight: None,
            })
            .collect();
        let unicode = super::text_pipeline::unicode_properties(py, &records)?;
        let lines = py.detach(|| {
            text_pipeline::group_text_lines(&records, height_threshold, distance, &unicode)
        });
        let output = PyList::empty(py);
        for line in lines {
            let row = PyDict::new(py);
            let spans = PyList::empty(py);
            row.set_item(
                pyo3::intern!(py, "bbox"),
                (line.bbox[0], line.bbox[1], line.bbox[2], line.bbox[3]),
            )?;
            row.set_item(pyo3::intern!(py, "rotation"), line.rotation)?;
            for span in line.spans {
                let value = PyDict::new(py);
                value.set_item(pyo3::intern!(py, "text"), span.text)?;
                spans.append(value)?;
            }
            row.set_item(pyo3::intern!(py, "spans"), spans)?;
            output.append(row)?;
        }
        Ok(output)
    }
    /// 返回 PDFium 原始字符数，绝不以去重/可见性过滤后的长度替代安全上限。
    fn raw_char_count(&self) -> usize {
        self.raw_count
    }
    /// 标准方向可直接使用完整自有字符；局部倾斜页保留现有整行水印判定路径。
    fn supports_span_matching(&self) -> bool {
        self.data.chars.iter().all(|ch| {
            let degrees = ch.rotation * (180.0 / std::f64::consts::PI);
            [0.0, 90.0, 180.0, 270.0]
                .iter()
                .any(|angle| (degrees - angle).abs() < 0.1)
        })
    }
    /// 仅从自有快照读取字符；Python 只提供小型 span 框与现有标点配置，不重新打包字符字典。
    #[pyo3(signature=(span_bboxes, median_height, stop_flags, start_flags, height_ratio, break_flags=None))]
    fn assign_spans(
        &self,
        py: Python<'_>,
        span_bboxes: Vec<[f64; 4]>,
        median_height: f64,
        stop_flags: Vec<String>,
        start_flags: Vec<String>,
        height_ratio: f64,
        break_flags: Option<Vec<String>>,
    ) -> PyResult<Option<Vec<Option<usize>>>> {
        if !self.supports_span_matching()
            || !median_height.is_finite()
            || !height_ratio.is_finite()
            || span_bboxes.iter().flatten().any(|value| !value.is_finite())
            || self.data.chars.iter().any(|ch| {
                !(ch.bbox[0] + ch.bbox[2]).is_finite()
                    || !(ch.bbox[1] + ch.bbox[3]).is_finite()
                    || ch
                        .tight
                        .is_some_and(|b| !(b[0] + b[2]).is_finite() || !(b[1] + b[3]).is_finite())
            })
        {
            SPAN_ASSIGNMENT_UNSUPPORTED.fetch_add(1, Ordering::Relaxed);
            return Ok(None);
        }
        let category = py.import("unicodedata")?.getattr("category")?;
        let mut properties: HashMap<String, u8> = HashMap::new();
        let mut tight = HashMap::new();
        if self.data.extended {
            for ch in &self.data.chars {
                if let Some(value) = ch.tight {
                    tight.insert(ch.index, value);
                }
            }
        }
        let mut chars = Vec::with_capacity(self.data.chars.len());
        for ch in &self.data.chars {
            let flags = if let Some(&flags) = properties.get(&ch.text) {
                flags
            } else {
                let text = pyo3::types::PyString::new(py, &ch.text);
                let mut flags = 0;
                if text.call_method0("isspace")?.extract::<bool>()? {
                    flags |= text_assignment::SPACE;
                }
                if ch.text.chars().count() == 1
                    && category
                        .call1((&text,))?
                        .extract::<String>()?
                        .starts_with('P')
                {
                    flags |= text_assignment::PUNCTUATION;
                }
                if break_flags
                    .as_ref()
                    .map_or(ch.text == "\r" || ch.text == "\n", |flags| {
                        flags.contains(&ch.text)
                    })
                {
                    flags |= text_assignment::BREAK;
                }
                if stop_flags.contains(&ch.text) {
                    flags |= text_assignment::STOP;
                }
                if start_flags.contains(&ch.text) {
                    flags |= text_assignment::START;
                }
                properties.insert(ch.text.clone(), flags);
                flags
            };
            chars.push(text_assignment::Character {
                bbox: ch.bbox,
                tight: tight.get(&ch.index).copied(),
                flags,
            });
        }
        let result = py
            .detach(|| text_assignment::assign(&chars, &span_bboxes, median_height, height_ratio));
        SPAN_ASSIGNMENT_CALLS.fetch_add(1, Ordering::Relaxed);
        Ok(Some(result))
    }
    /// 正文已有共享上下标侧车时，字符归属及内容构造连续完成，仅输出每个 span 的文本和 PUA 计数。
    #[pyo3(signature = (span_bboxes, median_height, stop_flags, start_flags, height_ratio, break_flags, modifiers, overlap_threshold, private_range, tight_spacing=None))]
    fn prepare_span_texts<'py>(
        &self,
        py: Python<'py>,
        span_bboxes: Vec<[f64; 4]>,
        median_height: f64,
        stop_flags: Vec<String>,
        start_flags: Vec<String>,
        height_ratio: f64,
        break_flags: Vec<String>,
        modifiers: HashMap<String, String>,
        overlap_threshold: f64,
        private_range: (u32, u32),
        tight_spacing: Option<Vec<bool>>,
    ) -> PyResult<Option<Bound<'py, PyList>>> {
        if !overlap_threshold.is_finite()
            || self
                .data
                .chars
                .iter()
                .any(|ch| !(ch.bbox[2] - ch.bbox[0]).is_finite())
        {
            return Ok(None);
        }
        let count = span_bboxes.len();
        // 旧宿主没有代码区域掩码，省略新增参数时保持历史文本组装行为。
        let tight_spacing = tight_spacing.unwrap_or_else(|| vec![false; count]);
        if tight_spacing.len() != count {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "Tight spacing mask must match span count",
            ));
        }
        let Some(assigned) = self.assign_spans(
            py,
            span_bboxes,
            median_height,
            stop_flags,
            start_flags,
            height_ratio,
            Some(break_flags.clone()),
        )?
        else {
            return Ok(None);
        };
        let groups = text_content::groups(&assigned, count);
        let unicode = py.import("unicodedata")?;
        let category = unicode.getattr("category")?;
        let normalize = unicode.getattr("normalize")?;
        let mut spaces = HashSet::new();
        let mut ordinary = HashSet::new();
        let mut decimals = HashSet::new();
        let mut seen_chars = HashSet::new();
        for ch in &self.data.chars {
            for value in ch.text.chars() {
                if !seen_chars.insert(value) {
                    continue;
                }
                if pyo3::types::PyString::new(py, &value.to_string())
                    .call_method0("isspace")?
                    .extract::<bool>()?
                {
                    spaces.insert(value);
                }
                if !value.is_ascii() && !docvortex_core::text_spacing::is_cjk(value) {
                    let kind: String = category.call1((value.to_string(),))?.extract()?;
                    if kind.starts_with('L') || kind == "Nd" {
                        ordinary.insert(value);
                    }
                    if kind == "Nd" {
                        decimals.insert(value);
                    }
                }
            }
        }
        let mut compositions = HashMap::new();
        let mut seen_pairs = HashSet::new();
        for group in &groups {
            let members = text_content::ordered(&self.data, group);
            for pair in members.windows(2) {
                if let Some((base, modifier)) = text_content::composition_pair(
                    &self.data,
                    pair[0],
                    pair[1],
                    &modifiers,
                    overlap_threshold,
                ) {
                    let base = &self.data.chars[base].text;
                    let modifier = &self.data.chars[modifier].text;
                    let key = (base.clone(), modifier.clone());
                    if seen_pairs.insert(key.clone())
                        && category
                            .call1((base,))?
                            .extract::<String>()?
                            .starts_with('L')
                    {
                        let composed: String = normalize
                            .call1(("NFC", format!("{}{}", base, modifiers[modifier])))?
                            .extract()?;
                        if composed.chars().count() == 1 && &composed != base {
                            compositions.insert(key, composed);
                        }
                    }
                }
            }
        }
        let breaks: HashSet<String> = break_flags.into_iter().collect();
        let records = py.detach(|| {
            text_content::build(
                &self.data,
                &groups,
                &text_content::Rules {
                    ordinary: &ordinary,
                    decimals: &decimals,
                    tight_spacing: &tight_spacing,
                    modifiers: &modifiers,
                    threshold: overlap_threshold,
                    compositions: &compositions,
                    spaces: &spaces,
                    breaks: &breaks,
                    private_range,
                },
            )
        });
        let output = PyList::empty(py);
        for record in records {
            let text = match record.text {
                Some(value) => Some(pyo3::types::PyString::new(py, &value).call_method0("strip")?),
                None => None,
            };
            output.append((
                text,
                record.private_count,
                record.text_count,
                record.private_run,
            ))?;
        }
        SPAN_CONTENT_CALLS.fetch_add(1, Ordering::Relaxed);
        Ok(Some(output))
    }
    /// 在自有字符上计算可靠词界，仅返回源编号，避免 Flash 逐字符重复跨语言判定。
    fn tight_space_indices(&self, py: Python<'_>) -> PyResult<Vec<usize>> {
        let category = py.import("unicodedata")?.getattr("category")?;
        let mut ordinary = HashSet::new();
        let mut decimals = HashSet::new();
        let mut seen = HashSet::new();
        for ch in &self.data.chars {
            for value in ch.text.chars() {
                if !value.is_ascii()
                    && !docvortex_core::text_spacing::is_cjk(value)
                    && seen.insert(value)
                {
                    let kind: String = category.call1((value.to_string(),))?.extract()?;
                    if kind.starts_with('L') || kind == "Nd" {
                        ordinary.insert(value);
                    }
                    if kind == "Nd" {
                        decimals.insert(value);
                    }
                }
            }
        }
        Ok(py.detach(|| {
            (1..self.data.chars.len())
                .filter(|&index| {
                    docvortex_core::text_spacing::needs_space(
                        &self.data,
                        index - 1,
                        index,
                        &ordinary,
                        &decimals,
                    )
                })
                .map(|index| self.data.chars[index].index)
                .collect()
        }))
    }

    /// 为新鲜同源页面证据准备独立上下标记录，后续批次只传整数成员索引。
    fn prepare_script_evidence(
        &self,
        flags: &Bound<'_, PyAny>,
    ) -> PyResult<Option<super::script_snapshot::NativeScriptEvidence>> {
        super::script_snapshot::prepare(&self.data, flags)
    }
    /// 报告实际所有权和策略，区分 Flash 可见文本与公开页面原始文本视图。
    fn info(&self) -> (usize, usize, bool, bool) {
        (
            self.data.chars.len(),
            self.data.fonts.len(),
            self.data.extended,
            self.data.visible_only,
        )
    }
    /// 直接以快照字符及行成员 ID 生成粗体和装饰线证据，不逐字符往返 Python。
    fn detect_style_lines(
        &self,
        py: Python<'_>,
        rows: Vec<([f64; 4], i64, Vec<usize>)>,
        drawings: Vec<([f64; 4], f64)>,
        text_properties: &Bound<'_, PyAny>,
        font_bold: &Bound<'_, PyAny>,
        thresholds: [f64; 8],
        min_bold: usize,
    ) -> PyResult<Option<Vec<docvortex_core::inline_styles::Payload>>> {
        super::inline_styles::detect(
            py,
            &self.data,
            rows,
            drawings,
            text_properties,
            font_bold,
            thresholds,
            min_bold,
        )
    }
    /// 自有 canonical 记录直接组行；完成纯计算后才联合物化字符与引用它们的视觉行。
    fn prepare_visual_evidence<'py>(
        &self,
        py: Python<'py>,
        size: [f64; 2],
        rotation: i32,
        supported_angles: Vec<f64>,
    ) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyList>)> {
        // 剖析开关只统计私有管线的五个连续阶段，不影响正式解析结果。
        let profile = std::env::var_os("DOCVORTEX_PROFILE_VISUAL_EVIDENCE").is_some();
        let total_started = std::time::Instant::now();
        if size.iter().chain(&supported_angles).any(|v| !v.is_finite()) {
            return Err(PyValueError::new_err("nonfinite visual snapshot arguments"));
        }
        let normalize = py
            .import("docvortex.analyzers.native.pdf.typography")?
            .getattr("_normalized_font_family")?;
        let mut signatures = Vec::new();
        let mut signature_ids = HashMap::new();
        let mut family_ids = HashMap::new();
        let mut families = Vec::new();
        let mut font_info = HashMap::new();
        let mut records = Vec::with_capacity(self.data.chars.len());
        for ch in &self.data.chars {
            let font = &self.data.fonts[ch.font];
            if let std::collections::hash_map::Entry::Vacant(entry) = font_info.entry(ch.font) {
                let id = if font.name.is_empty() {
                    None
                } else {
                    let signature = (font.name.clone(), font.flags as i64);
                    let id = if let Some(&id) = signature_ids.get(&signature) {
                        id
                    } else {
                        let id = signatures.len();
                        let family: Option<String> =
                            normalize.call1((signature.clone(),))?.extract()?;
                        let next = family_ids.len();
                        families.push(family.map(|name| *family_ids.entry(name).or_insert(next)));
                        signature_ids.insert(signature.clone(), id);
                        signatures.push(signature);
                        id
                    };
                    Some(id)
                };
                entry.insert((
                    id,
                    (id.is_some() && font.weight > 0).then_some(font.weight as f64),
                ));
            }
            let (signature_id, font_weight) = font_info[&ch.font];
            records.push(TextChar {
                bbox: ch.bbox,
                text: ch.text.clone(),
                rotation: ch.rotation,
                font_id: ch.font,
                font_size: Some(font.size),
                signature_id,
                font_weight,
            });
        }
        let font_ns = total_started.elapsed();
        let unicode_started = std::time::Instant::now();
        let unicode = super::text_pipeline::unicode_properties(py, &records)?;
        let unicode_ns = unicode_started.elapsed();
        let runs_started = std::time::Instant::now();
        let runs = py.detach(|| {
            text_pipeline::prepare_visual_lines(
                &records,
                size,
                rotation,
                &supported_angles,
                &unicode,
                &families,
            )
        });
        let runs_ns = runs_started.elapsed();
        let geometry_started = std::time::Instant::now();
        let (geometry, chars) = self.geometry(py)?;
        let geometry_ns = geometry_started.elapsed();
        let materialize_started = std::time::Instant::now();
        let lines = super::text_pipeline::materialize_visual_runs(py, &chars, runs, &signatures)?;
        let materialize_ns = materialize_started.elapsed();
        if profile {
            record_visual_stage_stats([
                font_ns,
                unicode_ns,
                runs_ns,
                geometry_ns,
                materialize_ns,
                total_started.elapsed(),
            ]);
        }
        Ok((geometry, lines))
    }
}

#[pymethods]
impl NativeGeometryEvidence {
    /// 把同源行成员一次转换并累积到全文风险器；None 表示本行选择参考路径。
    #[pyo3(signature = (risk, runs, page, source, height, skip_y, indices, size, angle))]
    fn add_line(
        &self,
        py: Python<'_>,
        mut risk: pyo3::PyRefMut<'_, super::geometry_risk::NativeGeometryRisk>,
        mut runs: pyo3::PyRefMut<'_, super::geometry_risk::NativeGeometryRuns>,
        page: usize,
        source: i64,
        height: f64,
        skip_y: bool,
        indices: Vec<usize>,
        size: [f64; 2],
        angle: i32,
    ) -> Option<bool> {
        if indices.iter().any(|&index| index >= self.records.len())
            || !size.iter().all(|v| v.is_finite())
        {
            GEOMETRY_EVIDENCE_FALLBACKS.fetch_add(1, Ordering::Relaxed);
            return None;
        }
        let rows: Vec<_> = indices
            .iter()
            .map(|&index| self.records[index].source)
            .collect();
        let prepared = py.detach(|| geometry::source_rows(rows, size, angle));
        let mut entries: Vec<Entry> = Vec::with_capacity(indices.len());
        for (record, prepared) in indices
            .iter()
            .map(|&index| &self.records[index])
            .zip(prepared)
        {
            let Some(row) = prepared else {
                continue;
            };
            if !record.anchor || !record.font_size.is_finite() {
                continue;
            }
            let key: RunKey = (
                record.family.clone(),
                geometry_number_key(record.rounded_font_size),
                record.flags,
                record.weight,
                angle,
                record.script.clone(),
            );
            let run = runs.intern(key);
            entries.push((run, row.3, row.4, row.5, record.font_size));
        }
        GEOMETRY_EVIDENCE_LINES.fetch_add(1, Ordering::Relaxed);
        let result = risk.add_prepared(py, page, source, height, skip_y, entries);
        if result.is_none() {
            GEOMETRY_EVIDENCE_FALLBACKS.fetch_add(1, Ordering::Relaxed);
        }
        result
    }
}

/// 累计剖析中的视觉证据阶段耗时，单位为纳秒。
fn record_visual_stage_stats(values: [std::time::Duration; 6]) {
    let mut slot = VISUAL_STAGE_NS
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let mut next = [0; 7];
    next[..6].copy_from_slice(&slot[..6]);
    for (index, value) in values.iter().enumerate() {
        next[index] = next[index].saturating_add(value.as_nanos() as u64);
    }
    next[6] = slot[6].saturating_add(1);
    *slot = next;
}

/// 暴露累计视觉证据剖析，最后一项为页面调用数。
#[pyfunction]
pub fn visual_evidence_stage_stats() -> [u64; 7] {
    let slot = VISUAL_STAGE_NS
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    *slot
}

/// 保存累计视觉证据剖析的阶段耗时和调用数。
static VISUAL_STAGE_NS: std::sync::Mutex<[u64; 7]> = std::sync::Mutex::new([0; 7]);
