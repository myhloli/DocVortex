//! The same library PDFium is adapted to ABI; it does not depend on Python and does not load the second runtime library.
use std::collections::HashMap;
use std::ffi::{c_double, c_float, c_int, c_uint, c_ulong, c_void};
pub mod classification;
pub mod drawing_lines;
pub mod fonts;
pub mod objects;
pub mod path_evidence;
pub mod paths;
pub mod text_colors;

/// Retain the native error classification and map it to the host exception by the binding layer.
#[derive(Debug)]
pub enum ReadError {
    InvalidInput(&'static str),
    Pdfium(&'static str),
    Allocation(&'static str),
}

#[repr(C)]
#[derive(Default)]
struct RectF {
    left: c_float,
    top: c_float,
    right: c_float,
    bottom: c_float,
}

pub type Box4 = (f64, f64, f64, f64);
pub type Point = (f64, f64);
pub type Record = (
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
pub type Fonts = Vec<(Vec<u8>, i32)>;

/// The character object address is converted to the Form number allocated by the host only during the survival of the text page.
///
/// # Safety
/// The caller must verify the ABI of FPDFText_GetTextObject, which holds the same library function, text page and PDFium locks.
pub unsafe fn read_char_form_owners(
    function: usize,
    handle: usize,
    count: usize,
    owners: HashMap<usize, usize>,
) -> Result<Vec<Option<usize>>, ReadError> {
    if function == 0 || handle == 0 || count > c_int::MAX as usize || owners.contains_key(&0) {
        return Err(ReadError::InvalidInput(
            "invalid PDFium character ownership arguments",
        ));
    }
    let getter: unsafe extern "system" fn(*mut c_void, c_int) -> *mut c_void =
        std::mem::transmute(function);
    let mut output = Vec::new();
    output
        .try_reserve(count)
        .map_err(|_| ReadError::Allocation("character ownership allocation failed"))?;
    for index in 0..count {
        output.push(
            owners
                .get(&(getter(handle as *mut c_void, index as c_int) as usize))
                .copied(),
        );
    }
    Ok(output)
}

/// Reading characters synchronously while the caller holds a runtime lock results in not owning any PDFium handle.
///
/// # Safety
/// The caller must verify function ABI, keep the same library functions, text pages and callbacks alive, and serialize all PDFium calls.
pub unsafe fn read_characters(
    addresses: Vec<usize>,
    handle: usize,
    count: usize,
    extended: bool,
) -> Result<(Vec<Record>, Fonts), ReadError> {
    if addresses.len() != 10 || addresses.contains(&0) || handle == 0 || count > c_int::MAX as usize
    {
        return Err(ReadError::InvalidInput("invalid PDFium bridge arguments"));
    }
    // Security boundary: only accept the ctypes function with a strong reference held by the adapter, and use the system ABI corresponding to the current platform FPDF_CALLCONV.
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
                return Err(ReadError::Pdfium("Failed to get charbox."));
            }
            let length = font_info(page, i, font_buffer.as_mut_ptr().cast(), 256, &mut flags);
            let mut long_buffer = Vec::new();
            let bytes: &[u8] = if length > 256 {
                long_buffer
                    .try_reserve_exact(length as usize)
                    .map_err(|_| ReadError::Allocation("PDFium font buffer allocation failed"))?;
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
                return Err(ReadError::InvalidInput("chr() arg not in range(0x110000)"));
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
