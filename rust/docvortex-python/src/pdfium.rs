//! Only borrow functions of the same PDFium and textpage within the existing Python guard, without owning or closing any handles.

use pyo3::exceptions::{PyException, PyMemoryError, PyValueError};
use pyo3::prelude::*;
pyo3::create_exception!(_native, PdfiumReadError, PyException);

use docvortex_core::extraction;
use docvortex_pdfium::{Fonts, ReadError, Record};

pub const RECORD_BATCH_SIZE: usize = 1024;

/// Batch reading of character Form numbers within the lifetime of the existing lock and text page, the result does not contain the borrowed address.
#[pyfunction]
pub fn read_pdfium_char_form_owners(
    function: usize,
    handle: usize,
    count: usize,
    owners: std::collections::HashMap<usize, usize>,
) -> PyResult<Vec<Option<usize>>> {
    unsafe { docvortex_pdfium::read_char_form_owners(function, handle, count, owners) }.map_err(
        |error| match error {
            ReadError::InvalidInput(message) => PyValueError::new_err(message),
            ReadError::Pdfium(message) => PdfiumReadError::new_err(message),
            ReadError::Allocation(message) => PyMemoryError::new_err(message),
        },
    )
}

/// Only point and endpoint indexes are constructed at the final boundary, and Python reuses the same point object to establish a straight line record.
#[pyfunction]
pub fn read_pdfium_subpaths(
    addresses: Vec<usize>,
    handle: usize,
) -> PyResult<Vec<(Vec<(f64, f64)>, Vec<(usize, usize)>, bool)>> {
    // The Python adapter verifies ABI and holds the runtime library, page and lock. This read does not release GIL.
    let records =
        unsafe { docvortex_pdfium::paths::read_subpaths(addresses, handle) }.map_err(|error| {
            match error {
                ReadError::InvalidInput(message) => PyValueError::new_err(message),
                ReadError::Pdfium(message) => PdfiumReadError::new_err(message),
                ReadError::Allocation(message) => PyMemoryError::new_err(message),
            }
        })?;
    Ok(records
        .into_iter()
        .map(|item| (item.points, item.lines, item.closed))
        .collect())
}

type ObjectRecord = (
    usize,
    (f64, f64, f64, f64, f64, f64),
    (f64, f64, f64, f64, f64, f64),
    usize,
    Option<(f64, f64, f64, f64)>,
);

/// Object records are materialized in fixed batches, preventing full page Python tuples from residing at the same time as final path evidence.
#[pyclass(module = "docvortex._native")]
pub struct PdfiumObjectBatches {
    records: std::vec::IntoIter<docvortex_pdfium::objects::Object>,
}

#[pymethods]
impl PdfiumObjectBatches {
    /// Iterate purely numerical values; the borrowed address must still be consumed within the page scope by the caller.
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    /// Each batch generates a maximum of 1024 object records and does not hold or release PDFium resources.
    fn __next__(&mut self) -> Option<Vec<ObjectRecord>> {
        let records: Vec<_> = self
            .records
            .by_ref()
            .take(RECORD_BATCH_SIZE)
            .map(|(handle, matrix, parent, depth, clip)| {
                (
                    handle,
                    matrix.into(),
                    parent.into(),
                    depth,
                    clip.map(Into::into),
                )
            })
            .collect();
        if records.is_empty() {
            None
        } else {
            Some(records)
        }
    }
}

/// Synchronous consumption object traversal; the return address can only be used immediately by the adapter holding the page.
#[pyfunction]
pub fn read_pdfium_objects(
    addresses: Vec<usize>,
    handle: usize,
    kind: i32,
    max_depth: usize,
) -> PyResult<PdfiumObjectBatches> {
    // ABI, runtime and pages are kept alive by the adapter, retaining GIL and existing runtime locks throughout.
    let records =
        unsafe { docvortex_pdfium::objects::read_objects(addresses, handle, kind, max_depth) }
            .map_err(|error| match error {
                ReadError::InvalidInput(message) => PyValueError::new_err(message),
                ReadError::Pdfium(message) => PdfiumReadError::new_err(message),
                ReadError::Allocation(message) => PyMemoryError::new_err(message),
            })?;
    Ok(PdfiumObjectBatches {
        records: records.into_iter(),
    })
}

type TextVisibilityRecord = (usize, bool, Option<(f64, f64, f64, f64)>);

/// A single TEXT traversal returns the drawing status and page visual cropping for Python to establish an address index.
#[pyfunction]
pub fn read_pdfium_text_visibility(
    addresses: Vec<usize>,
    handle: usize,
    frame: [f64; 4],
    rotation: i32,
    max_depth: usize,
) -> PyResult<Vec<TextVisibilityRecord>> {
    let records = unsafe {
        docvortex_pdfium::objects::read_text_visibility(
            addresses, handle, frame, rotation, max_depth,
        )
    }
    .map_err(|error| match error {
        ReadError::InvalidInput(message) => PyValueError::new_err(message),
        ReadError::Pdfium(message) => PdfiumReadError::new_err(message),
        ReadError::Allocation(message) => PyMemoryError::new_err(message),
    })?;
    Ok(records
        .into_iter()
        .map(|(address, visible, clip)| (address, visible, clip.map(Into::into)))
        .collect())
}

/// Record the numerical value of the Python materialization axis in fixed batches to avoid holding two large lists on the entire page at the same time.
#[pyclass(module = "docvortex._native")]
pub struct PdfiumDrawingLineBatches {
    records: std::vec::IntoIter<docvortex_pdfium::drawing_lines::Line>,
}

#[pymethods]
impl PdfiumDrawingLineBatches {
    /// Return the current iterator for synchronous consumption within the page scope.
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    /// A maximum of 1024 lines are returned each time, and the remaining records remain in Rust.
    fn __next__(&mut self) -> Option<Vec<docvortex_pdfium::drawing_lines::Line>> {
        let records: Vec<_> = self.records.by_ref().take(RECORD_BATCH_SIZE).collect();
        if records.is_empty() {
            None
        } else {
            Some(records)
        }
    }
}

/// Extract axis lines in batches using Path for simple strokes; return None for unsupported complex pages for reference implementation processing.
#[pyfunction]
pub fn read_pdfium_drawing_lines(
    addresses: Vec<usize>,
    handle: usize,
    bbox: [f64; 4],
    rotation: i32,
) -> PyResult<Option<PdfiumDrawingLineBatches>> {
    let records = unsafe {
        docvortex_pdfium::drawing_lines::read_fast_lines(addresses, handle, bbox, rotation)
    }
    .map_err(|error| match error {
        ReadError::InvalidInput(message) => PyValueError::new_err(message),
        ReadError::Pdfium(message) => PdfiumReadError::new_err(message),
        ReadError::Allocation(message) => PyMemoryError::new_err(message),
    })?;
    Ok(records.map(|items| PdfiumDrawingLineBatches {
        records: items.into_iter(),
    }))
}

/// The time taken to read the drawing line of the latest explicit analysis is exposed, in nanoseconds.
#[pyfunction]
pub fn pdfium_drawing_line_stage_stats() -> (u64, u64, u64) {
    docvortex_pdfium::drawing_lines::stage_stats()
}

type PathLineRecord = docvortex_pdfium::path_evidence::Line;
type PathInfoRecord = docvortex_pdfium::path_evidence::PathInfo;

/// Materializing Path plot lines in fixed batches, page evidence remains in Rust until Python is consumed.
#[pyclass(module = "docvortex._native")]
pub struct PdfiumPathLineBatches {
    records: std::vec::IntoIter<PathLineRecord>,
}

#[pymethods]
impl PdfiumPathLineBatches {
    /// Return the current batch iterator.
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    /// A maximum of 1024 unmerged axes are returned each time.
    fn __next__(&mut self) -> Option<Vec<PathLineRecord>> {
        let records: Vec<_> = self.records.by_ref().take(RECORD_BATCH_SIZE).collect();
        (!records.is_empty()).then_some(records)
    }
}

/// Materialize Path abstracts in fixed batches to avoid repeated saving of entire page records outside the boundary.
#[pyclass(module = "docvortex._native")]
pub struct PdfiumPathInfoBatches {
    records: std::vec::IntoIter<PathInfoRecord>,
}

#[pymethods]
impl PdfiumPathInfoBatches {
    /// Return the current batch iterator.
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    /// A maximum of 1024 path summaries are returned each time.
    fn __next__(&mut self) -> Option<Vec<PathInfoRecord>> {
        let records: Vec<_> = self.records.by_ref().take(RECORD_BATCH_SIZE).collect();
        (!records.is_empty()).then_some(records)
    }
}

/// A single traversal batch generates Path bilateral evidence; the complex state is also left in the native path for replication.
#[pyfunction]
pub fn read_pdfium_path_evidence(
    py: Python<'_>,
    addresses: Vec<usize>,
    handle: usize,
    frame: [f64; 4],
    rotation: i32,
    max_depth: usize,
    want_lines: bool,
    want_infos: bool,
) -> PyResult<Option<(PdfiumPathLineBatches, PdfiumPathInfoBatches)>> {
    if !want_lines && !want_infos {
        return Err(PyValueError::new_err(
            "PDFium path evidence requires one output side",
        ));
    }
    let hypot = py.import("math")?.getattr("hypot")?;
    let round = py.import("builtins")?.getattr("round")?;
    let records = unsafe {
        docvortex_pdfium::path_evidence::read_path_evidence_with_hypot(
            addresses,
            handle,
            frame,
            rotation,
            max_depth,
            want_lines,
            want_infos,
            // Maintain the bit-by-bit semantics of the interpreter math.hypot; exception input is returned by it to NaN/Inf without interrupting the same page.
            |x: f64, y: f64| {
                hypot
                    .call1((x, y))
                    .and_then(|value| value.extract::<f64>())
                    .unwrap_or(f64::NAN)
            },
            // Only open four corners Path enter this callback, retaining the critical value semantics of Python round (value, 3).
            |value: f64| {
                round
                    .call1((value, 3))
                    .and_then(|result| result.extract::<f64>())
                    .unwrap_or(f64::NAN)
            },
        )
    }
    .map_err(|error| match error {
        ReadError::InvalidInput(message) => PyValueError::new_err(message),
        ReadError::Pdfium(message) => PdfiumReadError::new_err(message),
        ReadError::Allocation(message) => PyMemoryError::new_err(message),
    })?;
    let (lines, infos) = records;
    Ok(Some((
        PdfiumPathLineBatches {
            records: lines.into_iter(),
        },
        PdfiumPathInfoBatches {
            records: infos.into_iter(),
        },
    )))
}

type VisualRecord = (
    u32,
    f64,
    usize,
    f64,
    i32,
    usize,
    Option<i32>,
    [f64; 4],
    Option<(f64, f64, f64, f64)>,
    Option<(f64, f64, f64, f64)>,
    Option<(f64, f64)>,
);

/// The original characters are retained in Rust, and the final coordinates are directly generated for each batch, eliminating the need for geometric repackaging of Python.
#[pyclass(module = "docvortex._native")]
pub struct PdfiumVisualCharacterBatches {
    records: std::vec::IntoIter<Record>,
    frame: [f64; 4],
    rounded: [f64; 2],
    angle: i32,
    extended: bool,
}

#[pymethods]
impl PdfiumVisualCharacterBatches {
    /// Value recording does not rely on the closed PDFium page, allowing delayed consumption.
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    /// To fuse coordinate transformations within a fixed batch, the original geometry is not first materialized into Python tuples.
    fn __next__(&mut self, py: Python<'_>) -> Option<Vec<VisualRecord>> {
        let chunk: Vec<_> = self.records.by_ref().take(RECORD_BATCH_SIZE).collect();
        if chunk.is_empty() {
            return None;
        }
        let (frame, rounded, angle, extended) =
            (self.frame, self.rounded, self.angle, self.extended);
        Some(py.detach(move || {
            let rows = chunk
                .iter()
                .map(|record| {
                    let selected = if record.1 == 0.0 { record.2 } else { record.3 };
                    // The selected box of each character has been verified in the reading phase, and the missing box will report an error before FFI returns.
                    (
                        selected.expect("validated character box").into(),
                        if extended {
                            record.2.map(Into::into)
                        } else {
                            None
                        },
                        if extended {
                            record.3.map(Into::into)
                        } else {
                            None
                        },
                        record.9.map(Into::into),
                    )
                })
                .collect();
            let visual = extraction::materialize(rows, frame, rounded, angle);
            chunk
                .into_iter()
                .zip(visual)
                .map(|(record, (layout, loose, tight, origin))| {
                    (
                        record.0,
                        record.1,
                        record.4,
                        record.5,
                        record.6,
                        record.7,
                        record.8,
                        layout,
                        loose.map(Into::into),
                        tight.map(Into::into),
                        origin.map(Into::into),
                    )
                })
                .collect()
        }))
    }
}

/// Borrow the current runtime library to read synchronously; subsequent coordinate conversion only relies on its own Rust data.
#[pyfunction]
pub fn read_pdfium_visual_batches(
    addresses: Vec<usize>,
    handle: usize,
    count: usize,
    extended: bool,
    frame: [f64; 4],
    angle: i32,
) -> PyResult<(PdfiumVisualCharacterBatches, Fonts)> {
    if !frame.iter().all(|value| value.is_finite()) || ![0, 90, 180, 270].contains(&angle) {
        return Err(PyValueError::new_err("invalid PDFium visual frame"));
    }
    let (records, fonts) = read_pdfium_data(addresses, handle, count, extended)?;
    Ok((
        PdfiumVisualCharacterBatches {
            records: records.into_iter(),
            frame,
            rounded: [
                (frame[2] - frame[0]).abs().ceil(),
                (frame[3] - frame[1]).abs().ceil(),
            ],
            angle,
            extended,
        },
        fonts,
    ))
}

/// Hold the pure numerical value read this time and construct Python tuples batch by batch to avoid the temporary object of the whole page and the final character being resident at the same time.
#[pyclass(module = "docvortex._native")]
pub struct PdfiumCharacterBatches {
    records: std::vec::IntoIter<Record>,
}

#[pymethods]
impl PdfiumCharacterBatches {
    /// The iterator only holds values, not PDFium handles or callbacks.
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    /// Each batch only constructs Python records within the upper limit, and the caller can release this batch of temporary tuples after consumption.
    fn __next__(&mut self) -> Option<Vec<Record>> {
        let chunk: Vec<_> = self.records.by_ref().take(RECORD_BATCH_SIZE).collect();
        if chunk.is_empty() {
            None
        } else {
            Some(chunk)
        }
    }
}

/// The original complete list entry of protocol 4 is retained, and the old binding caller does not need to change the return value processing.
#[pyfunction]
pub fn read_pdfium_chars(
    addresses: Vec<usize>,
    handle: usize,
    count: usize,
    extended: bool,
) -> PyResult<(Vec<Record>, Fonts)> {
    read_pdfium_data(addresses, handle, count, extended)
}

/// The result of the same PDFium call is saved in a numerical buffer and then handed over to Python in a bounded manner for materialization.
#[pyfunction]
pub fn read_pdfium_char_batches(
    py: Python<'_>,
    addresses: Vec<usize>,
    handle: usize,
    count: usize,
    extended: bool,
) -> PyResult<(Py<PdfiumCharacterBatches>, Fonts)> {
    let (records, fonts) = read_pdfium_data(addresses, handle, count, extended)?;
    Ok((
        Py::new(
            py,
            PdfiumCharacterBatches {
                records: records.into_iter(),
            },
        )?,
        fonts,
    ))
}

/// Preserve synchronization boundaries for holding locks and holding GIL, converting pure Rust errors into existing Python exceptions.
fn read_pdfium_data(
    addresses: Vec<usize>,
    handle: usize,
    count: usize,
    extended: bool,
) -> PyResult<(Vec<Record>, Fonts)> {
    // Safety conditions are guaranteed by the existing Python ABI probe and pdfium_guard, GIL is not released during the call.
    unsafe { docvortex_pdfium::read_characters(addresses, handle, count, extended) }.map_err(
        |error| match error {
            ReadError::InvalidInput(message) => PyValueError::new_err(message),
            ReadError::Pdfium(message) => PdfiumReadError::new_err(message),
            ReadError::Allocation(message) => PyMemoryError::new_err(message),
        },
    )
}
