//! PDFium Path 段读取与子路径组装；仅保存点及共享端点索引。

use crate::ReadError;
use std::ffi::{c_float, c_int, c_void};

pub type Point = (f64, f64);

#[derive(Default)]
pub struct Subpath {
    pub points: Vec<Point>,
    pub lines: Vec<(usize, usize)>,
    pub closed: bool,
}

/// 借用同库函数一次读完路径，曲线控制点保留，但不误作直线段。
///
/// # Safety
/// 调用方须验证 ABI，保持对象所属页面存活，并持有 PDFium 全局访问锁。
pub unsafe fn read_subpaths(
    addresses: Vec<usize>,
    handle: usize,
) -> Result<Vec<Subpath>, ReadError> {
    if addresses.len() != 5 || addresses.contains(&0) || handle == 0 {
        return Err(ReadError::InvalidInput(
            "invalid PDFium path bridge arguments",
        ));
    }
    let count: unsafe extern "system" fn(*mut c_void) -> c_int = std::mem::transmute(addresses[0]);
    let get: unsafe extern "system" fn(*mut c_void, c_int) -> *mut c_void =
        std::mem::transmute(addresses[1]);
    let point: unsafe extern "system" fn(*mut c_void, *mut c_float, *mut c_float) -> c_int =
        std::mem::transmute(addresses[2]);
    let kind: unsafe extern "system" fn(*mut c_void) -> c_int = std::mem::transmute(addresses[3]);
    let closes: unsafe extern "system" fn(*mut c_void) -> c_int = std::mem::transmute(addresses[4]);
    let mut output = Vec::new();
    let mut current = Subpath::default();
    let mut current_index = 0;
    for index in 0..count(handle as *mut c_void).max(0) {
        let segment = get(handle as *mut c_void, index);
        if segment.is_null() {
            continue;
        }
        let (mut x, mut y) = (0.0, 0.0);
        if point(segment, &mut x, &mut y) == 0 {
            continue;
        }
        let value = (f64::from(x), f64::from(y));
        let segment_type = kind(segment);
        let segment_closes = closes(segment) != 0;
        if segment_type == 2 {
            if !current.points.is_empty() {
                output.push(std::mem::take(&mut current));
            }
            current.points.push(value);
            current_index = 0;
        } else if current.points.is_empty() {
            current.points.push(value);
            current_index = 0;
        } else {
            let next = current.points.len();
            current.points.push(value);
            if segment_type == 0 {
                current.lines.push((current_index, next));
            }
            current_index = next;
        }
        if segment_closes {
            // 索引相同即同一 Python 点对象；NaN 也不能凭值比较额外产生闭合边。
            if current_index != 0 && current.points[current_index] != current.points[0] {
                current.lines.push((current_index, 0));
            }
            current.closed = true;
            current_index = 0;
        }
    }
    if !current.points.is_empty() {
        output.push(current);
    }
    Ok(output)
}
