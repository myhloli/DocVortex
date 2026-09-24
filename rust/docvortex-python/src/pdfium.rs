//! 仅在现有 Python guard 内借用同一 PDFium 的函数和 textpage，不拥有或关闭任何句柄。

use pyo3::exceptions::{PyException, PyMemoryError, PyValueError};
use pyo3::prelude::*;
use std::collections::HashMap;
use std::ffi::{c_double, c_float, c_int, c_uint, c_ulong, c_void};

pyo3::create_exception!(_native, PdfiumReadError, PyException);

#[repr(C)]
#[derive(Default)]
struct RectF {
    left: c_float,
    top: c_float,
    right: c_float,
    bottom: c_float,
}

type Box4 = (f64, f64, f64, f64);
type Point = (f64, f64);
type Record = (
    u32,
    f64,
    Option<Box4>,
    Option<Box4>,
    usize,
    f64,
    i32,
    usize,
    Option<i32>,
    Option<Point>,
);
type Fonts = Vec<(Vec<u8>, i32)>;

pub const RECORD_BATCH_SIZE: usize = 1024;

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

/// ABI 与地址由 Python 核验；两个入口始终持有 GIL，字体回调和外层 RLock 生命周期相同。
fn read_pdfium_data(
    addresses: Vec<usize>,
    handle: usize,
    count: usize,
    extended: bool,
) -> PyResult<(Vec<Record>, Fonts)> {
    if addresses.len() != 10 || addresses.contains(&0) || handle == 0 || count > c_int::MAX as usize
    {
        return Err(PyValueError::new_err("invalid PDFium bridge arguments"));
    }
    // 安全边界：仅接受适配器持有强引用的 ctypes 函数，使用当前平台 FPDF_CALLCONV 对应的 system ABI。
    unsafe {
        let unicode: unsafe extern "system" fn(*mut c_void, c_int) -> c_uint =
            std::mem::transmute(addresses[0]);
        let angle: unsafe extern "system" fn(*mut c_void, c_int) -> c_float =
            std::mem::transmute(addresses[1]);
        let loose_box: unsafe extern "system" fn(*mut c_void, c_int, *mut RectF) -> c_int =
            std::mem::transmute(addresses[2]);
        let tight_box: unsafe extern "system" fn(
            *mut c_void,
            c_int,
            *mut c_double,
            *mut c_double,
            *mut c_double,
            *mut c_double,
        ) -> c_int = std::mem::transmute(addresses[3]);
        let font_info: unsafe extern "system" fn(
            *mut c_void,
            c_int,
            *mut c_void,
            c_ulong,
            *mut c_int,
        ) -> c_ulong = std::mem::transmute(addresses[4]);
        let font_size: unsafe extern "system" fn(*mut c_void, c_int) -> c_double =
            std::mem::transmute(addresses[5]);
        let font_weight: unsafe extern "system" fn(*mut c_void, c_int) -> c_int =
            std::mem::transmute(addresses[6]);
        let text_object: unsafe extern "system" fn(*mut c_void, c_int) -> *mut c_void =
            std::mem::transmute(addresses[7]);
        let render_mode: unsafe extern "system" fn(*mut c_void) -> c_int =
            std::mem::transmute(addresses[8]);
        let char_origin: unsafe extern "system" fn(
            *mut c_void,
            c_int,
            *mut c_double,
            *mut c_double,
        ) -> c_int = std::mem::transmute(addresses[9]);
        let page = handle as *mut c_void;
        let mut records = Vec::with_capacity(count);
        let mut fonts: Fonts = Vec::new();
        let mut font_ids: HashMap<Vec<u8>, HashMap<i32, usize>> = HashMap::new();
        let mut modes = HashMap::<usize, i32>::new();
        let mut rect = RectF::default();
        let (mut left, mut right, mut bottom, mut top) = (0.0, 0.0, 0.0, 0.0);
        let (mut x, mut y) = (0.0, 0.0);
        let mut font_buffer = [0u8; 256];
        let mut flags = 0;
        for index in 0..count {
            let i = index as c_int;
            let code = unicode(page, i);
            let rotation = angle(page, i) as f64;
            let loose = if (rotation == 0.0 || extended) && loose_box(page, i, &mut rect) != 0 {
                Some((
                    rect.left as f64,
                    rect.bottom as f64,
                    rect.right as f64,
                    rect.top as f64,
                ))
            } else {
                None
            };
            let tight = if (rotation != 0.0 || extended)
                && tight_box(page, i, &mut left, &mut right, &mut bottom, &mut top) != 0
            {
                Some((left, bottom, right, top))
            } else {
                None
            };
            if (rotation == 0.0 && loose.is_none()) || (rotation != 0.0 && tight.is_none()) {
                return Err(PdfiumReadError::new_err("Failed to get charbox."));
            }
            let length = font_info(page, i, font_buffer.as_mut_ptr().cast(), 256, &mut flags);
            let mut long_buffer = Vec::new();
            let bytes: &[u8] = if length > 256 {
                long_buffer
                    .try_reserve_exact(length as usize)
                    .map_err(|_| PyMemoryError::new_err("PDFium font buffer allocation failed"))?;
                long_buffer.resize(length as usize, 0u8);
                font_info(page, i, long_buffer.as_mut_ptr().cast(), length, &mut flags);
                long_buffer.as_slice()
            } else if length > 0 {
                &font_buffer
            } else {
                &[]
            };
            let name = &bytes[..bytes.iter().position(|b| *b == 0).unwrap_or(bytes.len())];
            let font_flags = if length > 0 { flags } else { 0 };
            let font_id =
                if let Some(id) = font_ids.get(name).and_then(|items| items.get(&font_flags)) {
                    *id
                } else {
                    let id = fonts.len();
                    fonts.push((name.to_vec(), font_flags));
                    font_ids
                        .entry(name.to_vec())
                        .or_default()
                        .insert(font_flags, id);
                    id
                };
            let size = font_size(page, i);
            let weight = font_weight(page, i);
            if code > 0x10ffff {
                return Err(PyValueError::new_err("chr() arg not in range(0x110000)"));
            }
            let object = text_object(page, i);
            let address = object as usize;
            let mode = if object.is_null() {
                None
            } else {
                Some(*modes.entry(address).or_insert_with(|| render_mode(object)))
            };
            let origin = if char_origin(page, i, &mut x, &mut y) != 0 {
                Some((x, y))
            } else {
                None
            };
            records.push((
                code, rotation, loose, tight, font_id, size, weight, address, mode, origin,
            ));
        }
        Ok((records, fonts))
    }
}
