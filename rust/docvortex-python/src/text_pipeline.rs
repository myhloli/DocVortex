//! Read the original character object into its own data, and construct a compatible Python output in one go after consecutive grouping of lines.
use docvortex_core::text_pipeline::{self, TextChar, UnicodeProperties};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use std::collections::{HashMap, HashSet};
use std::sync::{Mutex, OnceLock};

static UNICODE_CACHE: OnceLock<Mutex<HashMap<char, u8>>> = OnceLock::new();

/// Read the Unicode attribute corresponding to the interpreter version for different non-ASCII characters in the page.
pub(crate) fn unicode_properties(
    py: Python<'_>,
    chars: &[TextChar],
) -> PyResult<UnicodeProperties> {
    let mut properties = UnicodeProperties::default();
    let cache = UNICODE_CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    let mut missing = Vec::new();
    {
        // Only bounded code point attributes are cached, never Python or page objects are held.
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

/// Only ordinary records are received; custom mapping, geometry or numerical values are returned to the reference path before executing the algorithm.
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
            // The original reference implementation retains the int/bool type of box coordinates; only the float coordinates of the native PDF are allowed to enter the calculation.
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
        // Small integers can be entered into double precision calculations accurately, and large integers are returned to the reference path before any conversion.
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

/// Check font nesting values to avoid comparing user-defined objects in advance to introduce additional side effects.
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

/// Distinguish between native numerical values and user-defined conversion and comparison behaviors.
fn plain_number(value: &Bound<'_, PyAny>) -> bool {
    value.is_exact_instance_of::<PyFloat>()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyBool>()
}

/// Directly consume the original character list, retaining characters, font references and all optional source fields.
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

/// Sharing the basic row output materialization logic ensures that group row members refer to the same materialized characters and fonts.
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
        // The public dictionary preserves the key insertion order of the reference implementation, avoiding JSON and iteration behavior changes.
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

/// The basic group rows and Flash visual segmentation are completed continuously, and only the final run and original character references are materialized.
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

/// The materialized vision run is only combined at the final boundary for common list entry and self-owned Rust snapshots.
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

/// The signature is deduplicated and read according to the font object, and the font family normalization only calls the existing rules when the signature appears for the first time.
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
