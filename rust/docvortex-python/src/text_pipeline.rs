//! 将原字符对象读入自有数据，连续组行后一次性构造兼容 Python 输出。
use docvortex_core::text_pipeline::{self, TextChar, UnicodeProperties};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use std::collections::{HashMap, HashSet};
use std::sync::{Mutex, OnceLock};

static UNICODE_CACHE: OnceLock<Mutex<HashMap<char, u8>>> = OnceLock::new();

/// 为页面中不同的非 ASCII 字符读取解释器版本对应的 Unicode 属性。
pub(crate) fn unicode_properties(
    py: Python<'_>,
    chars: &[TextChar],
) -> PyResult<UnicodeProperties> {
    let mut properties = UnicodeProperties::default();
    let cache = UNICODE_CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    let mut missing = Vec::new();
    {
        // 仅缓存有界的码点属性，绝不持有 Python 或页面对象。
        let cached = cache.lock().unwrap_or_else(|error| error.into_inner());
        for record in chars {
            for ch in record.text.chars().filter(|ch| !ch.is_ascii()) {
                if let std::collections::hash_map::Entry::Vacant(entry) = properties.0.entry(ch) {
                    if let Some(flags) = cached.get(&ch) {
                        entry.insert(*flags);
                    } else {
                        entry.insert(0);
                        missing.push(ch);
                    }
                }
            }
        }
    }
    if !missing.is_empty() {
        let category = py.import("unicodedata")?.getattr("category")?;
        for ch in missing {
            let text = PyString::new(py, &ch.to_string());
            let flags = u8::from(text.call_method0("isalnum")?.extract::<bool>()?)
                | (u8::from(text.call_method0("isdigit")?.extract::<bool>()?) << 1)
                | (u8::from(category.call1((&text,))?.extract::<String>()? == "Sm") << 2)
                | (u8::from(text.call_method0("isspace")?.extract::<bool>()?) << 3)
                | (u8::from(text.call_method0("isprintable")?.extract::<bool>()?) << 4)
                | (u8::from(text.call_method0("isdecimal")?.extract::<bool>()?) << 5);
            properties.0.insert(ch, flags);
        }
        let mut cached = cache.lock().unwrap_or_else(|error| error.into_inner());
        for (&ch, &flags) in &properties.0 {
            if cached.len() >= 16384 {
                break;
            }
            cached.insert(ch, flags);
        }
    }
    Ok(properties)
}

/// 仅接收自有普通记录；自定义映射、几何或数值在执行算法前返回参考路径。
fn read_chars(py: Python<'_>, chars: &Bound<'_, PyList>) -> PyResult<Option<Vec<TextChar>>> {
    if !chars.is_exact_instance_of::<PyList>() {
        return Ok(None);
    }
    let bbox_type = py
        .import("docvortex.document.pdf.text._contracts")?
        .getattr("Bbox")?;
    let mut records = Vec::with_capacity(chars.len());
    let mut previous_font: Option<Bound<'_, PyAny>> = None;
    let mut font_id = 0;
    let mut checked_fonts = HashSet::new();
    for value in chars.iter() {
        if !value.is_exact_instance_of::<PyDict>() {
            return Ok(None);
        }
        let font = value.get_item("font")?;
        let bbox = value.get_item("bbox")?;
        let text = value.get_item("char")?;
        let rotation = value.get_item("rotation")?;
        if !font.is_exact_instance_of::<PyDict>()
            || !bbox.get_type().is(&bbox_type)
            || !text.is_exact_instance_of::<PyString>()
            || !plain_number(&rotation)
        {
            return Ok(None);
        }
        if checked_fonts.insert(font.as_ptr() as usize) && !plain_font_value(&font, 0)? {
            return Ok(None);
        }
        let coordinates = bbox.getattr("bbox")?;
        if !coordinates.is_exact_instance_of::<PyList>()
            && !coordinates.is_exact_instance_of::<PyTuple>()
        {
            return Ok(None);
        }
        if coordinates.len()? != 4 {
            return Ok(None);
        }
        for i in 0..4 {
            // 原参考实现保留框坐标的 int/bool 类型；只让原生 PDF 的 float 坐标进入计算。
            if !coordinates.get_item(i)?.is_exact_instance_of::<PyFloat>() {
                return Ok(None);
            }
        }
        let Ok(raw_text) = text.extract::<String>() else {
            return Ok(None);
        };
        let Ok(raw_bbox) = coordinates.extract::<[f64; 4]>() else {
            return Ok(None);
        };
        // 小整数可精确进入双精度计算，大整数在任何转换前交回参考路径。
        if !rotation.is_exact_instance_of::<PyFloat>() && rotation.extract::<i32>().is_err() {
            return Ok(None);
        }
        let Ok(raw_rotation) = rotation.extract::<f64>() else {
            return Ok(None);
        };
        if raw_bbox.iter().any(|v| !v.is_finite()) || !raw_rotation.is_finite() {
            return Ok(None);
        }
        let size = font.get_item("size").ok();
        let font_size = match size {
            Some(value) if plain_number(&value) => value.extract::<f64>().ok(),
            _ => None,
        };
        if let Some(previous) = &previous_font {
            if !font.eq(previous)? {
                font_id += 1;
                previous_font = Some(font);
            }
        } else {
            previous_font = Some(font);
        }
        records.push(TextChar {
            bbox: raw_bbox,
            text: raw_text,
            rotation: raw_rotation,
            font_id,
            font_size,
            signature_id: None,
            font_weight: None,
        });
    }
    Ok(Some(records))
}

/// 检查字体嵌套值，避免提前比较用户自定义对象引入额外副作用。
fn plain_font_value(value: &Bound<'_, PyAny>, depth: usize) -> PyResult<bool> {
    if value.is_none() || plain_number(value) || value.is_exact_instance_of::<PyString>() {
        return Ok(true);
    }
    if depth >= 8 {
        return Ok(false);
    }
    if value.is_exact_instance_of::<PyDict>() {
        for (key, item) in value.cast::<PyDict>()?.iter() {
            if !plain_font_value(&key, depth + 1)? || !plain_font_value(&item, depth + 1)? {
                return Ok(false);
            }
        }
        return Ok(true);
    }
    if value.is_exact_instance_of::<PyList>() || value.is_exact_instance_of::<PyTuple>() {
        for item in value.try_iter()? {
            if !plain_font_value(&item?, depth + 1)? {
                return Ok(false);
            }
        }
        return Ok(true);
    }
    Ok(false)
}

/// 区分原生数值与用户定义的转换、比较行为。
fn plain_number(value: &Bound<'_, PyAny>) -> bool {
    value.is_exact_instance_of::<PyFloat>()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyBool>()
}

/// 直接消费原字符列表，保留字符、字体引用和所有可选来源字段。
#[pyfunction]
pub fn group_text_lines<'py>(
    py: Python<'py>,
    chars: &Bound<'py, PyList>,
    height_threshold: f64,
    distance: f64,
) -> PyResult<Option<Bound<'py, PyList>>> {
    let Some(records) = read_chars(py, chars)? else {
        return Ok(None);
    };
    let unicode = unicode_properties(py, &records)?;
    let lines = py
        .detach(|| text_pipeline::group_text_lines(&records, height_threshold, distance, &unicode));
    materialize_grouped_lines(py, chars, lines).map(Some)
}

/// 共用基础行输出物化逻辑，保证组行成员引用同次物化的字符与字体。
pub(crate) fn materialize_grouped_lines<'py>(
    py: Python<'py>,
    chars: &Bound<'py, PyList>,
    lines: Vec<text_pipeline::TextLine>,
) -> PyResult<Bound<'py, PyList>> {
    let bbox_type = py
        .import("docvortex.document.pdf.text._contracts")?
        .getattr("Bbox")?;
    let output = PyList::empty(py);
    for line in lines {
        let line_value = PyDict::new(py);
        let spans = PyList::empty(py);
        // 公共字典保留参考实现的键插入顺序，避免 JSON 和迭代行为改变。
        line_value.set_item("spans", &spans)?;
        line_value.set_item("bbox", bbox_type.call1((line.bbox.to_vec(),))?)?;
        let line_first = chars.get_item(line.spans[0].start)?;
        line_value.set_item("rotation", line_first.get_item("rotation")?)?;
        for span in line.spans {
            let value = PyDict::new(py);
            let first = chars.get_item(span.start)?;
            let last = chars.get_item(span.end - 1)?;
            value.set_item("bbox", bbox_type.call1((span.bbox.to_vec(),))?)?;
            value.set_item("text", span.text)?;
            value.set_item("font", first.get_item("font")?)?;
            value.set_item("chars", chars.get_slice(span.start, span.end))?;
            value.set_item("char_start_idx", first.get_item("char_idx")?)?;
            value.set_item("char_end_idx", last.get_item("char_idx")?)?;
            value.set_item("rotation", first.get_item("rotation")?)?;
            value.set_item("url", "")?;
            value.set_item("superscript", span.superscript)?;
            value.set_item("subscript", span.subscript)?;
            spans.append(value)?;
        }
        output.append(line_value)?;
    }
    Ok(output)
}

/// 连续完成基础组行和 Flash 视觉切分，只物化最终 run 与原字符引用。
#[pyfunction]
pub fn prepare_visual_lines<'py>(
    py: Python<'py>,
    chars: &Bound<'py, PyList>,
    size: [f64; 2],
    page_rotation: i32,
    supported_angles: Vec<f64>,
) -> PyResult<Option<Bound<'py, PyList>>> {
    let Some(mut records) = read_chars(py, chars)? else {
        return Ok(None);
    };
    let Some((signatures, families)) = prepare_fonts(py, chars, &mut records)? else {
        return Ok(None);
    };
    if size.iter().any(|v| !v.is_finite()) || supported_angles.iter().any(|v| !v.is_finite()) {
        return Ok(None);
    }
    let unicode = unicode_properties(py, &records)?;
    let runs = py.detach(|| {
        text_pipeline::prepare_visual_lines(
            &records,
            size,
            page_rotation,
            &supported_angles,
            &unicode,
            &families,
        )
    });
    materialize_visual_runs(py, chars, runs, &signatures).map(Some)
}

/// 仅在最终边界联合物化视觉 run，供普通列表入口及自有 Rust 快照共用。
pub(crate) fn materialize_visual_runs<'py>(
    py: Python<'py>,
    chars: &Bound<'py, PyList>,
    runs: Vec<text_pipeline::VisualTextRun>,
    signatures: &[(String, i64)],
) -> PyResult<Bound<'py, PyList>> {
    let output = PyList::empty(py);
    for run in runs {
        let members = PyList::empty(py);
        for index in run.indices {
            members.append(chars.get_item(index)?)?;
        }
        let metrics = run.typography.map(
            |(height, width, winner, coverage, weight, emphasis, typography)| {
                (
                    height,
                    width,
                    winner.map(|index| signatures[index].clone()),
                    coverage,
                    weight,
                    emphasis,
                    typography,
                    run.typographic_scale,
                )
            },
        );
        output.append((
            run.text,
            (run.bbox[0], run.bbox[1], run.bbox[2], run.bbox[3]),
            run.angle,
            members,
            run.row_id,
            run.run_index,
            run.split,
            run.formula,
            run.coarse_fallback,
            metrics,
            run.paragraph_terminal,
        ))?;
    }
    Ok(output)
}

/// 按字体对象去重读取签名，字体族归一化只在签名首次出现时调用既有规则。
fn prepare_fonts(
    py: Python<'_>,
    chars: &Bound<'_, PyList>,
    records: &mut [TextChar],
) -> PyResult<Option<(Vec<(String, i64)>, Vec<Option<usize>>)>> {
    let normalize = py
        .import("docvortex.analyzers.native.pdf.typography")?
        .getattr("_normalized_font_family")?;
    let mut cache = HashMap::new();
    let mut signatures = Vec::new();
    let mut signature_ids = HashMap::new();
    let mut family_ids = HashMap::new();
    let mut families = Vec::new();
    for (value, record) in chars.iter().zip(records.iter_mut()) {
        let font = value.get_item("font")?;
        let pointer = font.as_ptr() as usize;
        if let Some(&(id, weight)) = cache.get(&pointer) {
            record.signature_id = id;
            record.font_weight = weight;
            continue;
        }
        let name = font.get_item("name").ok();
        let name = match name {
            None => String::new(),
            Some(name) if name.is_none() => String::new(),
            Some(name) if name.is_exact_instance_of::<PyString>() => match name.extract::<String>()
            {
                Ok(name) => name,
                Err(_) => return Ok(None),
            },
            _ => return Ok(None),
        };
        let mut identifier = None;
        let mut weight = None;
        if !name.is_empty() {
            let flags = match font.get_item("flags").ok() {
                None => 0,
                Some(flags) if flags.is_none() => 0,
                Some(flags)
                    if flags.is_exact_instance_of::<PyInt>()
                        || flags.is_exact_instance_of::<PyBool>() =>
                {
                    let Ok(flags) = flags.extract::<i64>() else {
                        return Ok(None);
                    };
                    flags
                }
                _ => return Ok(None),
            };
            let signature = (name, flags);
            let id = if let Some(id) = signature_ids.get(&signature) {
                *id
            } else {
                let id = signatures.len();
                let family: Option<String> = normalize.call1((signature.clone(),))?.extract()?;
                let next = family_ids.len();
                families.push(family.map(|name| *family_ids.entry(name).or_insert(next)));
                signature_ids.insert(signature.clone(), id);
                signatures.push(signature);
                id
            };
            identifier = Some(id);
            if let Ok(value) = font.get_item("weight") {
                if !value.is_none() && !plain_number(&value) {
                    return Ok(None);
                }
                if let Ok(value) = value.extract::<f64>() {
                    if value.is_finite() && value > 0.0 {
                        weight = Some(value);
                    }
                }
            }
        }
        cache.insert(pointer, (identifier, weight));
        record.signature_id = identifier;
        record.font_weight = weight;
    }
    Ok(Some((signatures, families)))
}
