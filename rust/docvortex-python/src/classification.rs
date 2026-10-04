//! Independent snapshots of raw classification statistics, font normalization is only performed once across boundaries/font.
use docvortex_core::text_classification::{self, Counts};
use docvortex_pdfium::ReadError;
use pyo3::{
    exceptions::{PyMemoryError, PyValueError},
    prelude::*,
    types::{PyBytes, PyDict},
};
use std::{
    collections::HashSet,
    sync::atomic::{AtomicU64, Ordering},
};

static CALLS: AtomicU64 = AtomicU64::new(0);

#[pyclass(frozen, module = "docvortex._native")]
pub struct NativeClassificationSnapshot {
    counts: Counts,
    names: Vec<String>,
}

#[pymethods]
impl NativeClassificationSnapshot {
    /// Output an independent variable dictionary, maintain the original font appearance order, and merge the same font names after normalization.
    fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let output = PyDict::new(py);
        for (name, value) in [
            ("null_char_count", self.counts.null),
            ("replacement_char_count", self.counts.replacement),
            ("control_char_count", self.counts.control),
            ("private_use_char_count", self.counts.private),
            ("unicode_map_error_count", self.counts.map_error),
        ] {
            output.set_item(name, value)?;
        }
        for (field, position) in [
            ("font_name_counts", 0),
            ("font_non_generated_char_counts", 1),
            ("font_non_generated_cjk_char_counts", 2),
        ] {
            let values = PyDict::new(py);
            // Each statistical dictionary is sorted by the characters that meet the conditions for the first time, and the order of first appearance of fonts cannot be shared.
            let mut indices: Vec<_> = (0..self.names.len()).collect();
            indices.sort_by_key(|&index| self.counts.font_first[index][position]);
            for index in indices {
                let name = &self.names[index];
                let (all, non_generated, cjk) = self.counts.fonts[index];
                let count = [all, non_generated, cjk][position];
                if name.is_empty() || count == 0 {
                    continue;
                }
                let previous = values
                    .get_item(name)?
                    .map(|v| v.extract::<usize>())
                    .transpose()?
                    .unwrap_or(0);
                values.set_item(name, previous + count)?;
            }
            output.set_item(field, values)?;
            if position == 0 {
                output.set_item("non_generated_char_count", self.counts.non_generated)?;
            }
        }
        Ok(output)
    }
}

/// Use the same library text page to read all the original records, release the PDFium dependency, and then perform pure Rust summary.
#[pyfunction]
pub fn read_pdfium_classification(
    py: Python<'_>,
    addresses: [usize; 4],
    handle: usize,
    count: usize,
    cjk_ranges: Vec<(u32, u32)>,
    allowed_controls: Vec<u32>,
    private_range: (u32, u32),
    normalize_font: &Bound<'_, PyAny>,
) -> PyResult<NativeClassificationSnapshot> {
    let (records, fonts) =
        unsafe { docvortex_pdfium::classification::read(addresses, handle, count) }.map_err(
            |error| match error {
                ReadError::InvalidInput(message) => PyValueError::new_err(message),
                ReadError::Pdfium(message) => super::pdfium::PdfiumReadError::new_err(message),
                ReadError::Allocation(message) => PyMemoryError::new_err(message),
            },
        )?;
    let names = fonts
        .iter()
        .map(|name| {
            let decoded = PyBytes::new(py, name).call_method1("decode", ("utf-8", "ignore"))?;
            normalize_font.call1((decoded,))?.extract::<String>()
        })
        .collect::<PyResult<Vec<_>>>()?;
    let allowed: HashSet<_> = allowed_controls.into_iter().collect();
    let counts = py.detach(|| {
        text_classification::count(&records, fonts.len(), &cjk_ranges, &allowed, private_range)
    });
    CALLS.fetch_add(1, Ordering::Relaxed);
    Ok(NativeClassificationSnapshot { counts, names })
}

/// Count the number of real original classification reads and report them separately from the canonical character extraction count.
#[pyfunction]
pub fn classification_snapshot_stats() -> u64 {
    CALLS.load(Ordering::Relaxed)
}
