//! 仅修正越界字重：嵌入 OpenType 的 OS/2 是源证据，平台替代字体不是。

use std::collections::HashMap;
use std::ffi::{c_int, c_void};

const MAX_FONT_BYTES: usize = 64 * 1024 * 1024;

type ObjectFont = unsafe extern "system" fn(*mut c_void) -> *mut c_void;
type IsEmbedded = unsafe extern "system" fn(*mut c_void) -> c_int;
type FontData = unsafe extern "system" fn(*mut c_void, *mut u8, usize, *mut usize) -> c_int;

/// 缓存只在一次文本页读取期间存活，按真实句柄区分同名但不同字重的字体。
pub(crate) struct FontWeights {
    object_font: ObjectFont,
    is_embedded: IsEmbedded,
    font_data: FontData,
    fonts: HashMap<usize, i32>,
    objects: HashMap<usize, i32>,
}

impl FontWeights {
    /// 接收宿主已验证的同库 ABI 地址，调用方继续负责句柄与共享锁的生命周期。
    ///
    /// # Safety
    /// 三个地址必须依次对应 GetFont、GetIsEmbedded、GetFontData，且非空并持续有效。
    pub(crate) unsafe fn new(addresses: &[usize]) -> Self {
        Self {
            object_font: std::mem::transmute(addresses[0]),
            is_embedded: std::mem::transmute(addresses[1]),
            font_data: std::mem::transmute(addresses[2]),
            fonts: HashMap::new(),
            objects: HashMap::new(),
        }
    }

    /// 保留正常字重及未知值；超过 OpenType 上限的估算必须由嵌入字库证实。
    ///
    /// # Safety
    /// object 必须是本次文本页的存活对象或空指针，且调用方持有 PDFium 共享锁。
    pub(crate) unsafe fn resolve(&mut self, weight: i32, object: *mut c_void) -> i32 {
        if weight <= 1000 {
            return weight;
        }
        if object.is_null() {
            return -1;
        }
        let address = object as usize;
        if let Some(value) = self.objects.get(&address) {
            return *value;
        }
        let font = (self.object_font)(object);
        let value = if font.is_null() {
            -1
        } else if let Some(value) = self.fonts.get(&(font as usize)) {
            *value
        } else {
            let value = self.embedded_weight(font).unwrap_or(-1);
            self.fonts.insert(font as usize, value);
            value
        };
        self.objects.insert(address, value);
        value
    }

    /// 有界读取源字体；读取失败、非嵌入字体或无可靠 OS/2 字重均返回未知。
    ///
    /// # Safety
    /// font 必须来自本次文本页的存活对象，并在同一 PDFium 共享锁内读取。
    unsafe fn embedded_weight(&self, font: *mut c_void) -> Option<i32> {
        if (self.is_embedded)(font) != 1 {
            return None;
        }
        let mut size = 0;
        if (self.font_data)(font, std::ptr::null_mut(), 0, &mut size) == 0
            || !(12..=MAX_FONT_BYTES).contains(&size)
        {
            return None;
        }
        let mut bytes = Vec::new();
        bytes.try_reserve_exact(size).ok()?;
        bytes.resize(size, 0);
        let mut written = 0;
        if (self.font_data)(font, bytes.as_mut_ptr(), bytes.len(), &mut written) == 0
            || written > bytes.len()
        {
            return None;
        }
        sfnt_weight(&bytes[..written])
    }
}

/// 只读取完整 SFNT 目录内的 OS/2.usWeightClass，拒绝截断、越界和非法字重。
fn sfnt_weight(bytes: &[u8]) -> Option<i32> {
    if !matches!(bytes.get(..4)?, b"\0\x01\0\0" | b"OTTO" | b"true") {
        return None;
    }
    let count = u16::from_be_bytes(bytes.get(4..6)?.try_into().ok()?) as usize;
    let directory = bytes.get(12..12 + count * 16)?;
    for entry in directory.chunks_exact(16) {
        if &entry[..4] != b"OS/2" {
            continue;
        }
        let offset = u32::from_be_bytes(entry[8..12].try_into().ok()?) as usize;
        let length = u32::from_be_bytes(entry[12..16].try_into().ok()?) as usize;
        let table = bytes.get(offset..offset.checked_add(length)?)?;
        let weight = u16::from_be_bytes(table.get(4..6)?.try_into().ok()?) as i32;
        return (1..=1000).contains(&weight).then_some(weight);
    }
    None
}

#[cfg(test)]
mod tests {
    use super::sfnt_weight;

    /// 构造最小独立 SFNT 记录，不以生产函数生成期望。
    fn font(weight: u16) -> Vec<u8> {
        let mut bytes = vec![0u8; 34];
        bytes[..6].copy_from_slice(&[0, 1, 0, 0, 0, 1]);
        bytes[12..16].copy_from_slice(b"OS/2");
        bytes[20..24].copy_from_slice(&28u32.to_be_bytes());
        bytes[24..28].copy_from_slice(&6u32.to_be_bytes());
        bytes[32..34].copy_from_slice(&weight.to_be_bytes());
        bytes
    }

    /// 正常、粗体及两端合法字重都按源值读取，兼容 TrueType 与 OpenType 标识。
    #[test]
    fn reads_valid_sfnt_weights() {
        for weight in [1, 400, 700, 1000] {
            for signature in [b"\0\x01\0\0", b"OTTO", b"true"] {
                let mut bytes = font(weight);
                bytes[..4].copy_from_slice(signature);
                assert_eq!(sfnt_weight(&bytes), Some(i32::from(weight)));
            }
        }
    }

    /// 不完整目录/表、越界偏移、缺失表、非 SFNT 与非法字重均不能伪造加粗证据。
    #[test]
    fn rejects_untrusted_font_data() {
        let good = font(400);
        for end in 0..good.len() {
            assert_eq!(sfnt_weight(&good[..end]), None);
        }
        for weight in [0, 1001, u16::MAX] {
            assert_eq!(sfnt_weight(&font(weight)), None);
        }
        let mut invalid = good.clone();
        invalid[20..24].copy_from_slice(&u32::MAX.to_be_bytes());
        assert_eq!(sfnt_weight(&invalid), None);
        invalid = good.clone();
        invalid[24..28].copy_from_slice(&u32::MAX.to_be_bytes());
        assert_eq!(sfnt_weight(&invalid), None);
        invalid = good.clone();
        invalid[12..16].copy_from_slice(b"head");
        assert_eq!(sfnt_weight(&invalid), None);
        invalid[..4].copy_from_slice(b"ttcf");
        assert_eq!(sfnt_weight(&invalid), None);
    }
}
