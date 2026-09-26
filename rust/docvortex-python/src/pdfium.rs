//! 仅在现有 Python guard 内借用同一 PDFium 的函数和 textpage，不拥有或关闭任何句柄。

use pyo3::exceptions::{PyException, PyMemoryError, PyValueError};
use pyo3::prelude::*;
pyo3::create_exception!(_native, PdfiumReadError, PyException);

use docvortex_core::extraction;
use docvortex_pdfium::{Fonts, ReadError, Record};

pub const RECORD_BATCH_SIZE: usize = 1024;

/// 仅在最终边界构造点和端点索引，Python 复用同一点对象建立直线记录。
#[pyfunction]
pub fn read_pdfium_subpaths(
    addresses: Vec<usize>,
    handle: usize,
) -> PyResult<Vec<(Vec<(f64, f64)>, Vec<(usize, usize)>, bool)>> {
    // Python 适配器验证 ABI 并持有运行库、页面和锁，本次读取不释放 GIL。
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

/// 对象记录按固定批次物化，防止整页 Python 元组与最终路径证据同时驻留。
#[pyclass(module = "docvortex._native")]
pub struct PdfiumObjectBatches {
    records: std::vec::IntoIter<docvortex_pdfium::objects::Object>,
}

#[pymethods]
impl PdfiumObjectBatches {
    /// 迭代纯数值；借用地址仍须由调用方在页面作用域内消费。
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    /// 每批最多生成 1024 条对象记录，不持有或释放 PDFium 资源。
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

/// 同步消费对象遍历；返回地址只能由持有页面的适配器立即使用。
#[pyfunction]
pub fn read_pdfium_objects(
    addresses: Vec<usize>,
    handle: usize,
    kind: i32,
    max_depth: usize,
) -> PyResult<PdfiumObjectBatches> {
    // ABI、运行库和页面由适配器保持存活，全程保留 GIL 与既有运行时锁。
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

/// 原始字符保留在 Rust，每批直接生成最终坐标，省去 Python 几何重打包。
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
    /// 数值记录不依赖已关闭的 PDFium 页面，允许延后消费。
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    /// 在固定批次内融合坐标转换，原始几何不会先物化为 Python 元组。
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
                    // 读取阶段已验证每个字符的选定框，缺失框在 FFI 返回前报错。
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

/// 借用当前运行库同步读取；之后的坐标转换只依赖自有 Rust 数据。
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

/// 持有本次读取的纯数值，逐批构造 Python 元组，避免整页临时对象与最终字符同时常驻。
#[pyclass(module = "docvortex._native")]
pub struct PdfiumCharacterBatches {
    records: std::vec::IntoIter<Record>,
}

#[pymethods]
impl PdfiumCharacterBatches {
    /// 迭代器仅拥有数值，不拥有 PDFium 句柄或回调。
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    /// 每批只构造上限内的 Python 记录，调用方消费后即可释放这一批临时元组。
    fn __next__(&mut self) -> Option<Vec<Record>> {
        let chunk: Vec<_> = self.records.by_ref().take(RECORD_BATCH_SIZE).collect();
        if chunk.is_empty() {
            None
        } else {
            Some(chunk)
        }
    }
}

/// 保留协议 4 最初的完整列表入口，旧绑定调用方无需改变返回值处理。
#[pyfunction]
pub fn read_pdfium_chars(
    addresses: Vec<usize>,
    handle: usize,
    count: usize,
    extended: bool,
) -> PyResult<(Vec<Record>, Fonts)> {
    read_pdfium_data(addresses, handle, count, extended)
}

/// 同一次 PDFium 调用的结果按数值缓冲保存，随后有界地交给 Python 物化。
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

/// 保留持锁和持有 GIL 的同步边界，将纯 Rust 错误转换为已有 Python 异常。
fn read_pdfium_data(
    addresses: Vec<usize>,
    handle: usize,
    count: usize,
    extended: bool,
) -> PyResult<(Vec<Record>, Fonts)> {
    // 安全条件由现有 Python ABI 探测和 pdfium_guard 保证，调用期间不释放 GIL。
    unsafe { docvortex_pdfium::read_characters(addresses, handle, count, extended) }.map_err(
        |error| match error {
            ReadError::InvalidInput(message) => PyValueError::new_err(message),
            ReadError::Pdfium(message) => PdfiumReadError::new_err(message),
            ReadError::Allocation(message) => PyMemoryError::new_err(message),
        },
    )
}
