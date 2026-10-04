//! PDFium Path Segment reading and subpath assembly; only save points and shared endpoint indexes.

use crate::ReadError;
use std::ffi::{c_float, c_int, c_void};

pub type Point = (f64, f64);
type CountFn = unsafe extern "system" fn(*mut c_void) -> c_int;
type GetFn = unsafe extern "system" fn(*mut c_void, c_int) -> *mut c_void;
type PointFn = unsafe extern "system" fn(*mut c_void, *mut c_float, *mut c_float) -> c_int;
type SegmentFlagFn = unsafe extern "system" fn(*mut c_void) -> c_int;

/// Path segment reading ABI; all objects on the same page reuse function pointers and do not perform object-by-object reconstruction.
pub struct Api {
    pub(crate) count: CountFn,
    pub(crate) get: GetFn,
    pub(crate) point: PointFn,
    pub(crate) kind: SegmentFlagFn,
    pub(crate) closes: SegmentFlagFn,
}

#[derive(Default)]
pub struct Subpath {
    pub points: Vec<Point>,
    pub lines: Vec<(usize, usize)>,
    pub closed: bool,
}

/// Borrowing the same library function to read the path at once, the curve control points are retained, but no straight line segments are mistakenly made.
///
/// # Safety
/// The caller must verify ABI, keep the page to which the object belongs alive, and hold the PDFium global access lock.
pub unsafe fn read_subpaths(
    addresses: Vec<usize>,
    handle: usize,
) -> Result<Vec<Subpath>, ReadError> {
    if addresses.len() != 5 || addresses.contains(&0) || handle == 0 {
        return Err(ReadError::InvalidInput(
            "invalid PDFium path bridge arguments",
        ));
    }
    let api = Api {
        count: std::mem::transmute::<usize, CountFn>(addresses[0]),
        get: std::mem::transmute::<usize, GetFn>(addresses[1]),
        point: std::mem::transmute::<usize, PointFn>(addresses[2]),
        kind: std::mem::transmute::<usize, SegmentFlagFn>(addresses[3]),
        closes: std::mem::transmute::<usize, SegmentFlagFn>(addresses[4]),
    };
    read_subpaths_with(&api, handle)
}

/// Decode a Path using verified ABI; retain the skip semantics of the reference implementation when segment read fails.
///
/// # Safety
/// The caller must keep the page to which the object belongs alive and hold the PDFium global access lock.
pub unsafe fn read_subpaths_with(api: &Api, handle: usize) -> Result<Vec<Subpath>, ReadError> {
    let mut output = Vec::new();
    let mut current = Subpath::default();
    let mut current_index = 0;
    for index in 0..(api.count)(handle as *mut c_void).max(0) {
        let segment = (api.get)(handle as *mut c_void, index);
        if segment.is_null() {
            continue;
        }
        let (mut x, mut y) = (0.0, 0.0);
        if (api.point)(segment, &mut x, &mut y) == 0 {
            continue;
        }
        let value = (f64::from(x), f64::from(y));
        let segment_type = (api.kind)(segment);
        let segment_closes = (api.closes)(segment) != 0;
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
            // The same index means the same Python point object; NaN cannot generate additional closed edges based on value comparison.
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
