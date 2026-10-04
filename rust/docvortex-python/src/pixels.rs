//! The binding borrows only the immutable Python bytes; it does not access the PDFium or mutable image when releasing the GIL.
use pyo3::{exceptions::PyValueError, prelude::*, types::PyBytes};

/// Convert an area of the independent bitmap directly into continuous BGR bytes, and the encoding continues to use the original OpenCV configuration.
#[pyfunction]
pub fn crop_bitmap_bgr<'py>(
    py: Python<'py>,
    data: &Bound<'py, PyBytes>,
    width: usize,
    height: usize,
    stride: usize,
    mode: &str,
    bbox: (usize, usize, usize, usize),
    angle: u16,
) -> PyResult<(Bound<'py, PyBytes>, usize, usize)> {
    let input = data.as_bytes();
    let (output, width, height) = py
        .detach(|| {
            docvortex_core::pixels::crop_bgr(input, width, height, stride, mode, bbox, angle)
        })
        .map_err(PyValueError::new_err)?;
    Ok((PyBytes::new(py, &output), width, height))
}
