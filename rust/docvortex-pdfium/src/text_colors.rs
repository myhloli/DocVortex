//! Read the transparency determined by the first character of the object from the same host library textpage, without saving the handle or loading the runtime library.
use crate::ReadError;
use std::ffi::{c_int, c_uint, c_void};

/// Following the original fill/stroke query sequence, the failure return value is conservatively regarded as invisible.
/// # Safety
/// The host must verify the ABI of both addresses, keep textpage/library/callback alive, and hold the PDFium global lock.
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
