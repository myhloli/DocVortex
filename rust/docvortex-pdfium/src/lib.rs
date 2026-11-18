//! 同库 PDFium ABI 适配；不依赖 Python，不加载第二份运行库。
use std::collections::HashMap;
use std::ffi::{c_double, c_float, c_int, c_uint, c_ulong, c_void};
pub mod classification;
pub mod drawing_lines;
mod font_weight;
pub mod fonts;
pub mod marked_content;
pub mod objects;
pub mod path_evidence;
pub mod paths;
pub mod text_colors;

/// 保留原生错误分类，由绑定层映射到宿主异常。
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
    Option<bool>,
);
pub type Fonts = Vec<(Vec<u8>, i32)>;

/// 只在文本页存活期间把字符对象地址转换为宿主分配的 Form 编号。
///
/// # Safety
/// 调用方须核验 FPDFText_GetTextObject 的 ABI，持有同库函数、文本页和 PDFium 锁。
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

// 字段名、函数签名和宿主地址顺序使用同一份声明，避免增删符号时两处漂移。
macro_rules! character_functions {
    ($($name:ident: $signature:ty),+ $(,)?) => {
        struct CharacterFunctions {
            $($name: $signature),+
        }

        impl CharacterFunctions {
            /// 将宿主核验过的同库地址解包成具名函数。
            ///
            /// # Safety
            /// 每个地址须符合相应的函数签名，且在整次读取期间保持有效。
            unsafe fn from_addresses(addresses: &[usize]) -> Result<Self, ReadError> {
                let &[$($name),+] = addresses else {
                    return Err(ReadError::InvalidInput("invalid PDFium bridge arguments"));
                };
                if addresses.contains(&0) {
                    return Err(ReadError::InvalidInput("invalid PDFium bridge arguments"));
                }
                Ok(Self {
                    $($name: std::mem::transmute::<usize, $signature>($name)),+
                })
            }
        }
    }
}

character_functions! {
    unicode: unsafe extern "system" fn(*mut c_void, c_int) -> c_uint,
    angle: unsafe extern "system" fn(*mut c_void, c_int) -> c_float,
    loose_box: unsafe extern "system" fn(*mut c_void, c_int, *mut RectF) -> c_int,
    tight_box: unsafe extern "system" fn(
        *mut c_void,
        c_int,
        *mut c_double,
        *mut c_double,
        *mut c_double,
        *mut c_double,
    ) -> c_int,
    font_info:
        unsafe extern "system" fn(*mut c_void, c_int, *mut c_void, c_ulong, *mut c_int) -> c_ulong,
    font_size: unsafe extern "system" fn(*mut c_void, c_int) -> c_double,
    font_weight: unsafe extern "system" fn(*mut c_void, c_int) -> c_int,
    text_object: unsafe extern "system" fn(*mut c_void, c_int) -> *mut c_void,
    render_mode: unsafe extern "system" fn(*mut c_void) -> c_int,
    char_origin:
        unsafe extern "system" fn(*mut c_void, c_int, *mut c_double, *mut c_double) -> c_int,
    is_generated: unsafe extern "system" fn(*mut c_void, c_int) -> c_int,
    object_font: font_weight::ObjectFont,
    is_embedded: font_weight::IsEmbedded,
    font_data: font_weight::FontData,
}

/// 在调用方持有运行时锁期间同步读取字符，结果不拥有任何 PDFium 句柄。
///
/// # Safety
/// 调用方须验证函数 ABI，保持同库函数、文本页及回调存活，并串行化全部 PDFium 调用。
pub unsafe fn read_characters(
    addresses: Vec<usize>,
    handle: usize,
    count: usize,
    extended: bool,
) -> Result<(Vec<Record>, Fonts), ReadError> {
    if handle == 0 || count > c_int::MAX as usize {
        return Err(ReadError::InvalidInput("invalid PDFium bridge arguments"));
    }
    // 安全边界：仅接受适配器持有强引用的 ctypes 函数，使用当前平台 FPDF_CALLCONV 对应的 system ABI。
    unsafe {
        let functions = CharacterFunctions::from_addresses(&addresses)?;
        let page = handle as *mut c_void;
        let mut font_weights = font_weight::FontWeights::new(
            functions.object_font,
            functions.is_embedded,
            functions.font_data,
        );
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
            let code = (functions.unicode)(page, i);
            let rotation = (functions.angle)(page, i) as f64;
            let loose = if (rotation == 0.0 || extended)
                && (functions.loose_box)(page, i, &mut rect) != 0
            {
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
                && (functions.tight_box)(page, i, &mut left, &mut right, &mut bottom, &mut top) != 0
            {
                Some((left, bottom, right, top))
            } else {
                None
            };
            if (rotation == 0.0 && loose.is_none()) || (rotation != 0.0 && tight.is_none()) {
                return Err(ReadError::Pdfium("Failed to get charbox."));
            }
            let length =
                (functions.font_info)(page, i, font_buffer.as_mut_ptr().cast(), 256, &mut flags);
            let mut long_buffer = Vec::new();
            let bytes: &[u8] = if length > 256 {
                long_buffer
                    .try_reserve_exact(length as usize)
                    .map_err(|_| ReadError::Allocation("PDFium font buffer allocation failed"))?;
                long_buffer.resize(length as usize, 0u8);
                (functions.font_info)(page, i, long_buffer.as_mut_ptr().cast(), length, &mut flags);
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
            let size = (functions.font_size)(page, i);
            let object = (functions.text_object)(page, i);
            let weight = font_weights.resolve((functions.font_weight)(page, i), object);
            if code > 0x10ffff {
                return Err(ReadError::InvalidInput("chr() arg not in range(0x110000)"));
            }
            let address = object as usize;
            let mode = if object.is_null() {
                None
            } else {
                Some(
                    *modes
                        .entry(address)
                        .or_insert_with(|| (functions.render_mode)(object)),
                )
            };
            let origin = if (functions.char_origin)(page, i, &mut x, &mut y) != 0 {
                Some((x, y))
            } else {
                None
            };
            records.push((
                code,
                rotation,
                loose,
                tight,
                font_id,
                size,
                weight,
                address,
                mode,
                origin,
                match (functions.is_generated)(page, i) {
                    0 => Some(false),
                    1 => Some(true),
                    _ => None,
                },
            ));
        }
        Ok((records, fonts))
    }
}
