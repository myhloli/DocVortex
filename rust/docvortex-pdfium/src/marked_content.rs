//! 在页面锁内批量复制内容标记，结果只持有自有字符串和 MCID。

use crate::ReadError;
use std::collections::HashMap;
use std::ffi::{c_char, c_int, c_ulong, c_void};

pub type Mark = (
    String,
    i32,
    Option<Vec<u8>>,
    Option<Vec<u8>>,
    Option<Vec<u8>>,
    Option<Vec<u8>>,
    usize,
);
pub type MarkedObject = (i32, Vec<usize>);
pub type MarkedContent = (Vec<Mark>, Vec<MarkedObject>);

/// 按 PDFium 的字节长度约定读取 UTF-16，拒绝异常大的属性。
unsafe fn read_string<F>(mut getter: F) -> Result<Option<String>, ReadError>
where
    F: FnMut(*mut u16, c_ulong, *mut c_ulong) -> c_int,
{
    let mut length = 0;
    if getter(std::ptr::null_mut(), 0, &mut length) == 0 || length == 0 {
        return Ok(None);
    }
    if length > 4 * 1024 * 1024 || length % 2 != 0 {
        return Err(ReadError::InvalidInput(
            "invalid marked-content string length",
        ));
    }
    let mut buffer = Vec::new();
    buffer
        .try_reserve(length as usize / 2)
        .map_err(|_| ReadError::Allocation("marked-content string allocation failed"))?;
    buffer.resize(length as usize / 2, 0u16);
    let capacity = length;
    if getter(buffer.as_mut_ptr(), capacity, &mut length) == 0 || length > capacity {
        return Err(ReadError::Pdfium(
            "marked-content string changed during read",
        ));
    }
    buffer.truncate(length as usize / 2);
    if buffer.last() == Some(&0) {
        buffer.pop();
    }
    Ok(Some(String::from_utf16_lossy(&buffer)))
}

/// 原样复制 PDF 字符串字节，由对象解析器按 PDFDocEncoding 或 BOM 解码。
unsafe fn read_blob<F>(mut getter: F) -> Result<Option<Vec<u8>>, ReadError>
where
    F: FnMut(*mut u8, c_ulong, *mut c_ulong) -> c_int,
{
    let mut length = 0;
    if getter(std::ptr::null_mut(), 0, &mut length) == 0 {
        return Ok(None);
    }
    if length > 4 * 1024 * 1024 {
        return Err(ReadError::InvalidInput(
            "invalid marked-content blob length",
        ));
    }
    let mut buffer = Vec::new();
    buffer
        .try_reserve(length as usize)
        .map_err(|_| ReadError::Allocation("marked-content blob allocation failed"))?;
    buffer.resize(length as usize, 0u8);
    let capacity = length;
    if getter(buffer.as_mut_ptr(), capacity, &mut length) == 0 || length > capacity {
        return Err(ReadError::Pdfium("marked-content blob changed during read"));
    }
    buffer.truncate(length as usize);
    Ok(Some(buffer))
}

/// 同步读取调用方提供的页面对象，返回完整标记栈及最内层 MCID。
///
/// # Safety
/// 调用方必须校验全部 ABI，并保持同一 PDFium 运行库、页面对象和锁存活。
pub unsafe fn read(addresses: Vec<usize>, objects: Vec<usize>) -> Result<MarkedContent, ReadError> {
    if addresses.len() != 6 || addresses.contains(&0) || objects.contains(&0) {
        return Err(ReadError::InvalidInput(
            "invalid marked-content bridge arguments",
        ));
    }
    let mcid: unsafe extern "system" fn(*mut c_void) -> c_int = std::mem::transmute(addresses[0]);
    let count: unsafe extern "system" fn(*mut c_void) -> c_int = std::mem::transmute(addresses[1]);
    let mark: unsafe extern "system" fn(*mut c_void, c_ulong) -> *mut c_void =
        std::mem::transmute(addresses[2]);
    let name: unsafe extern "system" fn(*mut c_void, *mut u16, c_ulong, *mut c_ulong) -> c_int =
        std::mem::transmute(addresses[3]);
    let string: unsafe extern "system" fn(
        *mut c_void,
        *const c_char,
        *mut u8,
        c_ulong,
        *mut c_ulong,
    ) -> c_int = std::mem::transmute(addresses[4]);
    let integer: unsafe extern "system" fn(*mut c_void, *const c_char, *mut c_int) -> c_int =
        std::mem::transmute(addresses[5]);
    let mut output = Vec::new();
    let mut sequence_ids = HashMap::new();
    let mut definitions = Vec::new();
    let mut string_bytes = 0usize;
    output
        .try_reserve(objects.len())
        .map_err(|_| ReadError::Allocation("marked-content objects allocation failed"))?;
    for object in objects {
        let object = object as *mut c_void;
        let length = count(object);
        if !(0..=1024).contains(&length) {
            return Err(ReadError::InvalidInput("invalid marked-content nesting"));
        }
        let mut marks = Vec::new();
        for index in 0..length {
            let handle = mark(object, index as c_ulong);
            if handle.is_null() {
                return Err(ReadError::Pdfium("missing content mark"));
            }
            if let Some(index) = sequence_ids.get(&(handle as usize)) {
                marks.push(*index);
                continue;
            }
            let label = read_string(|buffer, size, used| name(handle, buffer, size, used))?
                .unwrap_or_default();
            let mut marker = -1;
            integer(handle, b"MCID\0".as_ptr().cast(), &mut marker);
            let actual = read_blob(|buffer, size, used| {
                string(handle, b"ActualText\0".as_ptr().cast(), buffer, size, used)
            })?;
            let language = read_blob(|buffer, size, used| {
                string(handle, b"Lang\0".as_ptr().cast(), buffer, size, used)
            })?;
            let kind = read_blob(|buffer, size, used| {
                string(handle, b"Type\0".as_ptr().cast(), buffer, size, used)
            })?;
            let subtype = read_blob(|buffer, size, used| {
                string(handle, b"Subtype\0".as_ptr().cast(), buffer, size, used)
            })?;
            let next_id = sequence_ids.len();
            string_bytes += label.len()
                + [&actual, &language, &kind, &subtype]
                    .into_iter()
                    .map(|value| value.as_ref().map_or(0, Vec::len))
                    .sum::<usize>();
            if string_bytes > 64 * 1024 * 1024 {
                return Err(ReadError::InvalidInput(
                    "marked-content string budget exceeded",
                ));
            }
            sequence_ids.insert(handle as usize, next_id);
            definitions.push((label, marker, actual, language, kind, subtype, next_id));
            marks.push(next_id);
        }
        output.push((mcid(object), marks));
    }
    Ok((definitions, output))
}
