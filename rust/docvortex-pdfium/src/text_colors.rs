//! 从宿主同库 textpage 读取按对象首次字符确定的透明度，不保存句柄或加载运行库。
use crate::ReadError;
use std::ffi::{c_int, c_uint, c_void};

/// 遵循原 fill/stroke 查询顺序，失败返回值保守视为不可见。
/// # Safety
/// 宿主须验证两个地址的 ABI，保持 textpage/库/回调存活，并持有 PDFium 全局锁。
pub unsafe fn read_visible(
    addresses: [usize; 2],
    handle: usize,
    queries: &[(usize, Option<i32>)],
) -> Result<Vec<bool>, ReadError> {
    if addresses.contains(&0)
        || handle == 0
        || queries
            .iter()
            .any(|(index, _)| *index > c_int::MAX as usize)
    {
        return Err(ReadError::InvalidInput(
            "invalid text color bridge arguments",
        ));
    }
    type Color = unsafe extern "system" fn(
        *mut c_void,
        c_int,
        *mut c_uint,
        *mut c_uint,
        *mut c_uint,
        *mut c_uint,
    ) -> c_int;
    let readers: [Color; 2] = unsafe {
        [
            std::mem::transmute::<usize, Color>(addresses[0]),
            std::mem::transmute::<usize, Color>(addresses[1]),
        ]
    };
    let mut output = Vec::with_capacity(queries.len());
    let (mut red, mut green, mut blue, mut alpha) = (0, 0, 0, 0);
    for &(index, mode) in queries {
        let mut visible = false;
        for (position, reader) in readers.iter().enumerate() {
            if ((position == 0 && mode.is_some_and(|m| [0, 2, 4, 6].contains(&m)))
                || (position == 1 && mode.is_some_and(|m| [1, 2, 5, 6].contains(&m))))
                && unsafe {
                    reader(
                        handle as *mut c_void,
                        index as c_int,
                        &mut red,
                        &mut green,
                        &mut blue,
                        &mut alpha,
                    )
                } != 0
                && alpha > 0
            {
                visible = true;
            }
        }
        output.push(visible);
    }
    Ok(output)
}
