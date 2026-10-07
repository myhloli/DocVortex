//! 绑定只借用不可变 Python 字节；释放 GIL 时不访问 PDFium 或可变图像。
use pyo3::{exceptions::PyValueError, prelude::*, types::PyBytes};

/// 批量生成定长位集，绑定只物化每个字形的一个字节对象和宽高比。
#[pyfunction]
pub(super) fn glyph_masks<'py>(
    py: Python<'py>,
    data: &Bound<'py, PyBytes>,
    width: usize,
    height: usize,
    boxes: Vec<[i64; 4]>,
) -> PyResult<Vec<Option<(Bound<'py, PyBytes>, f64)>>> {
    if width.checked_mul(height) != Some(data.as_bytes().len())
        || boxes
            .iter()
            .flatten()
            .any(|v| v.unsigned_abs() > 1_000_000_000)
    {
        return Err(PyValueError::new_err(
            "invalid grayscale buffer or glyph bounds",
        ));
    }
    let input = data.as_bytes();
    let values = py.detach(|| docvortex_core::pixels::glyph_masks(input, width, height, &boxes));
    Ok(values
        .into_iter()
        .map(|v| v.map(|(bits, aspect)| (PyBytes::new(py, &bits), aspect)))
        .collect())
}

/// 按原阈值检查合成后的 RGB 字节，不创建整图差分和掩码副本。
#[pyfunction]
pub(super) fn blank_top_rgb(
    py: Python<'_>,
    data: &Bound<'_, PyBytes>,
    width: usize,
    height: usize,
) -> PyResult<f64> {
    if width.checked_mul(height).and_then(|n| n.checked_mul(3)) != Some(data.as_bytes().len()) {
        return Err(PyValueError::new_err("invalid RGB buffer"));
    }
    let input = data.as_bytes();
    Ok(py.detach(|| docvortex_core::pixels::blank_top_rgb(input, width, height)))
}

/// 灰度图保持原始字节和阈值，纯扫描释放 GIL。
#[pyfunction]
pub(super) fn blank_top_gray(
    py: Python<'_>,
    data: &Bound<'_, PyBytes>,
    width: usize,
    height: usize,
) -> PyResult<f64> {
    if width.checked_mul(height) != Some(data.as_bytes().len()) {
        return Err(PyValueError::new_err("invalid grayscale buffer"));
    }
    let input = data.as_bytes();
    Ok(py.detach(|| docvortex_core::pixels::blank_top_gray(input, width, height)))
}

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

/// 仅借用独立不可变字节，位图关闭或其他 PDFium 操作不会进入释放 GIL 的扫描阶段。
#[pyfunction]
pub(super) fn blank_top_bitmap(
    py: Python<'_>,
    data: &Bound<'_, PyBytes>,
    width: usize,
    height: usize,
    stride: usize,
    mode: &str,
) -> Option<f64> {
    let input = data.as_bytes();
    py.detach(|| docvortex_core::pixels::blank_top_bitmap(input, width, height, stride, mode))
}
