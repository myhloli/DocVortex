//! 分类所需的原始字符统计读取；不做去重、规范化或几何筛选。
use crate::ReadError;
use std::{
    collections::HashMap,
    ffi::{c_int, c_uint, c_ulong, c_void},
};

pub type RawRecord = (u32, bool, bool, usize);
pub type RawPage = (Vec<RawRecord>, Vec<Vec<u8>>);

/// 同步读取原始 Unicode、generated、map error 和字体字节；不保留 PDFium 指针。
///
/// # Safety
/// 调用方必须校验同库函数 ABI，并在整个调用期间持有运行时锁与有效文本页。
pub unsafe fn read(
    addresses: [usize; 4],
    handle: usize,
    count: usize,
) -> Result<RawPage, ReadError> {
    if addresses.contains(&0) || handle == 0 || count > c_int::MAX as usize {
        return Err(ReadError::InvalidInput(
            "invalid classification ABI or character count",
        ));
    }
    unsafe {
        let unicode: unsafe extern "system" fn(*mut c_void, c_int) -> c_uint =
            std::mem::transmute(addresses[0]);
        let generated: unsafe extern "system" fn(*mut c_void, c_int) -> c_int =
            std::mem::transmute(addresses[1]);
        let map_error: unsafe extern "system" fn(*mut c_void, c_int) -> c_int =
            std::mem::transmute(addresses[2]);
        let font_info: unsafe extern "system" fn(
            *mut c_void,
            c_int,
            *mut c_void,
            c_ulong,
            *mut c_int,
        ) -> c_ulong = std::mem::transmute(addresses[3]);
        let page = handle as *mut c_void;
        let mut records = Vec::new();
        records
            .try_reserve_exact(count)
            .map_err(|_| ReadError::Allocation("classification records allocation failed"))?;
        let mut fonts = Vec::new();
        let mut font_ids = HashMap::new();
        let mut buffer = Vec::<u8>::new();
        for index in 0..count {
            let index = index as c_int;
            let code = unicode(page, index);
            let is_generated = generated(page, index) == 1;
            let has_map_error = map_error(page, index) != 0;
            let mut flags = 0;
            let length = font_info(page, index, std::ptr::null_mut(), 0, &mut flags) as usize;
            buffer.clear();
            let name = if length == 0 {
                &[][..]
            } else {
                buffer.try_reserve_exact(length).map_err(|_| {
                    ReadError::Allocation("classification font buffer allocation failed")
                })?;
                buffer.resize(length, 0);
                let actual = font_info(
                    page,
                    index,
                    buffer.as_mut_ptr().cast(),
                    length as c_ulong,
                    &mut flags,
                );
                if actual == 0 {
                    &[][..]
                } else {
                    &buffer[..buffer
                        .iter()
                        .position(|&value| value == 0)
                        .unwrap_or(buffer.len())]
                }
            };
            let font = if let Some(&id) = font_ids.get(name) {
                id
            } else {
                let id = fonts.len();
                fonts.push(name.to_vec());
                font_ids.insert(name.to_vec(), id);
                id
            };
            records.push((code, is_generated, has_map_error, font));
        }
        Ok((records, fonts))
    }
}
