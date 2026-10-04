//! Page level own superscript and subscript input: the binding layer only receives the character index and does not read the Python character dictionary repeatedly.
use docvortex_core::{geometry, scripts, text_snapshot::TextSnapshot};
use pyo3::{exceptions::PyValueError, prelude::*};
use std::{
    collections::HashMap,
    sync::atomic::{AtomicU64, Ordering},
};

static BATCH_CALLS: AtomicU64 = AtomicU64::new(0);

#[pyclass(frozen, module = "docvortex._native")]
pub struct NativeScriptEvidence {
    records: Vec<scripts::Record>,
}

/// Obtain the current interpreter Unicode mark by unique text, and the original font equivalence class and extended geometry are prepared once in Rust.
pub fn prepare(
    data: &TextSnapshot,
    flags: &Bound<'_, PyAny>,
) -> PyResult<Option<NativeScriptEvidence>> {
    let mut tight = HashMap::new();
    let mut origins = HashMap::new();
    if data.extended {
        for ch in &data.chars {
            if let Some(value) = ch.tight {
                tight.insert(ch.index, value);
            }
            if let Some(value) = ch.origin {
                origins.insert(ch.index, value);
            }
        }
    }
    let mut properties = HashMap::new();
    let mut fonts = HashMap::new();
    let mut prepared_fonts = vec![None; data.fonts.len()];
    let mut records = Vec::with_capacity(data.chars.len());
    for ch in &data.chars {
        let t = tight.get(&ch.index).copied();
        let o = origins.get(&ch.index).copied();
        if ch
            .bbox
            .iter()
            .chain(t.iter().flatten())
            .chain(o.iter().flatten())
            .any(|v| !v.is_finite())
        {
            return Ok(None);
        }
        let mut flag = match properties.get(&ch.text) {
            Some(&value) => value,
            None => {
                let value: u32 = flags.call1((&ch.text,))?.extract()?;
                properties.insert(ch.text.clone(), value);
                value
            }
        };
        // canonical font ID can directly reuse equivalent classes to avoid copying and re-hashing the font name for each character.
        let font_id = if let Some(id) = prepared_fonts[ch.font] {
            id
        } else {
            let font = &data.fonts[ch.font];
            let next = fonts.len() as i64;
            let id = if font.name.is_empty() {
                -1
            } else {
                *fonts
                    .entry((font.name.clone(), font.flags, font.weight))
                    .or_insert(next)
            };
            prepared_fonts[ch.font] = Some(id);
            id
        };
        let t = geometry::normalize(t, true);
        if flag & 3 == 0 && t.is_some() && o.is_some() {
            flag |= 256;
        }
        records.push((
            geometry::normalize(Some(ch.bbox), true).unwrap_or([0.0; 4]),
            t,
            o,
            flag,
            font_id,
        ));
    }
    Ok(Some(NativeScriptEvidence { records }))
}

#[pymethods]
impl NativeScriptEvidence {
    /// According to the boundary classification of the original formula segmentation, the source index allows duplication and rearrangement; out-of-bounds or wrong boundaries are clearly reported.
    fn classify_indices(
        &self,
        py: Python<'_>,
        indices: Vec<usize>,
        offsets: Vec<usize>,
    ) -> PyResult<Vec<Option<Vec<u8>>>> {
        if indices.iter().any(|&i| i >= self.records.len())
            || offsets.first() != Some(&0)
            || offsets.last() != Some(&indices.len())
            || offsets.windows(2).any(|p| p[0] >= p[1])
        {
            return Err(PyValueError::new_err(
                "invalid owned script indices or offsets",
            ));
        }
        let output = py.detach(|| {
            offsets
                .windows(2)
                .map(|p| {
                    scripts::classify(
                        indices[p[0]..p[1]]
                            .iter()
                            .map(|&i| self.records[i])
                            .collect(),
                    )
                })
                .collect()
        });
        BATCH_CALLS.fetch_add(1, Ordering::Relaxed);
        Ok(output)
    }
}

/// Report the number of batch calls of the real page's own script for full playback to confirm that the new path has been hit.
#[pyfunction]
pub fn script_snapshot_stats() -> u64 {
    BATCH_CALLS.load(Ordering::Relaxed)
}
