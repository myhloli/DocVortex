//! 独立图像字节的 Rust 绑定，释放 GIL 时不访问 Python 数组或 PDFium。
use pyo3::{exceptions::PyValueError, prelude::*, types::PyBytes};

/// 验证尺寸后在 Rust 内核标记八连通域，输出标签字节和独立统计。
#[pyfunction]
pub fn image_components<'py>(
    py: Python<'py>,
    data: &Bound<'py, PyBytes>,
    width: usize,
    height: usize,
) -> PyResult<(Bound<'py, PyBytes>, Vec<[i32; 5]>)> {
    let input = data.as_bytes();
    let (labels, stats) = py
        .detach(|| docvortex_core::image_numeric::components(input, width, height))
        .map_err(PyValueError::new_err)?;
    Ok((PyBytes::new(py, &labels), stats))
}

/// 轮廓扫描只读取独立字节，点列表在返回 Python 时一次物化。
#[pyfunction]
pub fn image_contours(
    py: Python<'_>,
    data: &Bound<'_, PyBytes>,
    width: usize,
    height: usize,
    external: bool,
) -> PyResult<Vec<Vec<[i32; 2]>>> {
    let input = data.as_bytes();
    py.detach(|| docvortex_core::image_numeric::contours(input, width, height, external))
        .map_err(PyValueError::new_err)
}

/// 系数由公共参考规则生成，热卷积在释放 GIL 的 Rust 核心执行。
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn image_resize<'py>(
    py: Python<'py>,
    data: &Bound<'py, PyBytes>,
    width: usize,
    height: usize,
    channels: usize,
    depth: u8,
    target_w: usize,
    target_h: usize,
    mode: u8,
    xindices: Vec<Vec<usize>>,
    xweights: Vec<Vec<f32>>,
    yindices: Vec<Vec<usize>>,
    yweights: Vec<Vec<f32>>,
) -> PyResult<Bound<'py, PyBytes>> {
    if ![1, 2, 4].contains(&depth) || !(1..=4).contains(&channels) || target_w == 0 || target_h == 0
    {
        return Err(PyValueError::new_err("invalid resize shape or depth"));
    }
    let input = data.as_bytes();
    let output = py
        .detach(|| {
            docvortex_core::image_numeric::resize(
                input, width, height, channels, depth, target_w, target_h, mode, &xindices,
                &xweights, &yindices, &yweights,
            )
        })
        .map_err(PyValueError::new_err)?;
    Ok(PyBytes::new(py, &output))
}

/// 逆映射只消费独立字节及矩阵数值，释放 GIL 后执行像素热循环。
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn image_warp<'py>(
    py: Python<'py>,
    data: &Bound<'py, PyBytes>,
    width: usize,
    height: usize,
    channels: usize,
    depth: u8,
    target_w: usize,
    target_h: usize,
    mode: u8,
    replicate: bool,
    value: f32,
    inverse: [f64; 9],
    affine: bool,
) -> PyResult<Bound<'py, PyBytes>> {
    if ![1, 2, 4].contains(&depth) || !(1..=4).contains(&channels) || mode > 2 {
        return Err(PyValueError::new_err("invalid warp mode"));
    }
    let input = data.as_bytes();
    let output = py
        .detach(|| {
            docvortex_core::image_numeric::warp(
                input, width, height, channels, depth, target_w, target_h, mode, replicate, value,
                &inverse, affine,
            )
        })
        .map_err(PyValueError::new_err)?;
    Ok(PyBytes::new(py, &output))
}

/// 凸包坐标物化后在 Rust 内计算旋转卡尺，不保留 Python 数组。
#[pyfunction]
pub fn image_minimum_rectangle(py: Python<'_>, data: &Bound<'_, PyBytes>) -> PyResult<[f32; 5]> {
    let bytes = data.as_bytes();
    if bytes.len() % 8 != 0 {
        return Err(PyValueError::new_err("invalid float32 point buffer"));
    }
    py.detach(|| {
        // 一次物化连续坐标，避免为大连通区域逐点创建 Python float/list 对象。
        let hull: Vec<[f32; 2]> = bytes
            .as_chunks::<8>()
            .0
            .iter()
            .map(|chunk| {
                [
                    f32::from_ne_bytes(chunk[..4].try_into().unwrap()),
                    f32::from_ne_bytes(chunk[4..].try_into().unwrap()),
                ]
            })
            .collect();
        if hull.iter().flatten().any(|v| !v.is_finite()) {
            return Err("non-finite hull coordinate");
        }
        Ok(docvortex_core::image_numeric::minimum_rectangle(
            &docvortex_core::image_numeric::convex_hull(&hull),
        ))
    })
    .map_err(PyValueError::new_err)
}

/// 二维八位矩形极值释放 GIL，并验证核尺寸及自有缓冲区。
#[pyfunction]
pub fn image_morphology<'py>(
    py: Python<'py>,
    data: &Bound<'py, PyBytes>,
    width: usize,
    height: usize,
    kw: usize,
    kh: usize,
    dilate: bool,
) -> PyResult<Bound<'py, PyBytes>> {
    let input = data.as_bytes();
    let output = py
        .detach(|| docvortex_core::image_numeric::morphology(input, width, height, kw, kh, dilate))
        .map_err(PyValueError::new_err)?;
    Ok(PyBytes::new(py, &output))
}

/// 批量线段内核只读取自有字节，输出为独立八位图像。
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn image_lines<'py>(
    py: Python<'py>,
    data: &Bound<'py, PyBytes>,
    width: usize,
    height: usize,
    channels: usize,
    lines: Vec<[i32; 4]>,
    value: u8,
    thickness: usize,
) -> PyResult<Bound<'py, PyBytes>> {
    let input = data.as_bytes();
    let output = py
        .detach(|| {
            docvortex_core::image_drawing::lines(
                input, width, height, channels, &lines, value, thickness,
            )
        })
        .map_err(PyValueError::new_err)?;
    Ok(PyBytes::new(py, &output))
}

/// 多边形只传递整数坐标和尺寸，扫描及填充在释放 GIL 后执行。
#[pyfunction]
pub fn image_polygons<'py>(
    py: Python<'py>,
    width: usize,
    height: usize,
    polygons: Vec<Vec<[i32; 2]>>,
    value: u8,
) -> PyResult<Bound<'py, PyBytes>> {
    let output = py
        .detach(|| docvortex_core::image_drawing::polygons(width, height, &polygons, value))
        .map_err(PyValueError::new_err)?;
    Ok(PyBytes::new(py, &output))
}

/// 消元只处理有界的小型独立数值矩阵，奇异输入返回 None。
#[pyfunction]
pub fn image_solve(
    py: Python<'_>,
    matrix: Vec<Vec<f64>>,
    rhs: Vec<f64>,
) -> PyResult<Option<Vec<f64>>> {
    let n = matrix.len();
    if n == 0
        || n > 16
        || rhs.len() != n
        || matrix.iter().any(|row| row.len() != n)
        || matrix.iter().flatten().chain(&rhs).any(|v| !v.is_finite())
    {
        return Err(PyValueError::new_err("invalid small linear system"));
    }
    Ok(py.detach(|| docvortex_core::image_numeric::linear_solve(&matrix, &rhs)))
}
