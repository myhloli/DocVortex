//! 绑定只借用不可变 Python 字节；释放 GIL 时不访问 PDFium 或可变图像。
use pyo3::{exceptions::PyValueError, prelude::*, types::PyBytes};

/// 将独立位图的一块区域直接转换为连续 BGR 字节，编码继续使用原 OpenCV 配置。
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
