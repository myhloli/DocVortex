//! Axial lines are extracted in batches on supported simple stroke pages, and complex objects are implemented by Python reference.

use crate::{objects, ReadError};
use std::ffi::{c_float, c_int, c_uint, c_void};
use std::sync::Mutex;
use std::time::Instant;

pub type Line = ((f64, f64), (f64, f64), (f64, f64, f64, f64), f64, i32);
static LAST_STAGE_NS: Mutex<(u64, u64, u64)> = Mutex::new((0, 0, 0));

/// Returns the number of nanoseconds since the last time drawing line profiling was enabled for object reading, line segment reading, and geometry calculation.
pub fn stage_stats() -> (u64, u64, u64) {
    *LAST_STAGE_NS
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

type Segment = *mut c_void;
type Object = *mut c_void;
type CountFn = unsafe extern "system" fn(Object) -> c_int;
type GetFn = unsafe extern "system" fn(Object, c_int) -> Segment;
type PointFn = unsafe extern "system" fn(Segment, *mut c_float, *mut c_float) -> c_int;
type SegmentFlagFn = unsafe extern "system" fn(Segment) -> c_int;
type DrawModeFn = unsafe extern "system" fn(Object, *mut c_int, *mut c_int) -> c_int;
type StrokeColorFn =
    unsafe extern "system" fn(Object, *mut c_uint, *mut c_uint, *mut c_uint, *mut c_uint) -> c_int;
type StrokeWidthFn = unsafe extern "system" fn(Object, *mut c_float) -> c_int;

struct Api {
    count: CountFn,
    get: GetFn,
    point: PointFn,
    kind: SegmentFlagFn,
    closes: SegmentFlagFn,
    draw_mode: DrawModeFn,
    stroke_color: StrokeColorFn,
    stroke_width: StrokeWidthFn,
}

/// Transform points according to Python page visual coordinate rules, and the order of operations is consistent with the reference path.
fn visual_point(point: (f64, f64), bbox: [f64; 4], rotation: i32) -> (f64, f64) {
    let (x, y) = point;
    let [left, bottom, right, top] = bbox;
    match rotation {
        90 => (y - bottom, x - left),
        180 => (right - x, y - bottom),
        270 => (top - y, right - x),
        _ => (x - left, top - y),
    }
}

/// Apply the same axis and page cropping judgments to individual line segments as Python `_make_axis_drawing_line`.
fn axis_line(
    raw_start: (f64, f64),
    raw_end: (f64, f64),
    stroke_width: f64,
    bbox: [f64; 4],
    rotation: i32,
) -> Option<Line> {
    let start = visual_point(raw_start, bbox, rotation);
    let end = visual_point(raw_end, bbox, rotation);
    let (page_width, page_height) = if rotation == 90 || rotation == 270 {
        (bbox[3] - bbox[1], bbox[2] - bbox[0])
    } else {
        (bbox[2] - bbox[0], bbox[3] - bbox[1])
    };
    let (x0, y0) = start;
    let (x1, y1) = end;
    if ![x0, y0, x1, y1, stroke_width].iter().all(|v| v.is_finite()) {
        return None;
    }
    let delta_x = (x1 - x0).abs();
    let delta_y = (y1 - y0).abs();
    if delta_x >= delta_y && delta_y <= 1.0_f64.max(delta_x * 0.02) {
        let coordinate = (y0 + y1) / 2.0;
        if coordinate < 0.0 || coordinate > page_height {
            return None;
        }
        let main_start = 0.0_f64.max(x0.min(x1));
        let main_end = page_width.min(x0.max(x1));
        if main_end - main_start < 1.0 {
            return None;
        }
        let width = 0.0_f64.max(stroke_width).max(delta_y);
        let half_width = width / 2.0;
        return Some((
            (main_start, coordinate),
            (main_end, coordinate),
            (
                main_start,
                0.0_f64.max(coordinate - half_width),
                main_end,
                page_height.min(coordinate + half_width),
            ),
            width,
            0,
        ));
    }
    if delta_y > delta_x && delta_x <= 1.0_f64.max(delta_y * 0.02) {
        let coordinate = (x0 + x1) / 2.0;
        if coordinate < 0.0 || coordinate > page_width {
            return None;
        }
        let main_start = 0.0_f64.max(y0.min(y1));
        let main_end = page_height.min(y0.max(y1));
        if main_end - main_start < 1.0 {
            return None;
        }
        let width = 0.0_f64.max(stroke_width).max(delta_x);
        let half_width = width / 2.0;
        return Some((
            (coordinate, main_start),
            (coordinate, main_end),
            (
                0.0_f64.max(coordinate - half_width),
                main_start,
                page_width.min(coordinate + half_width),
                main_end,
            ),
            width,
            1,
        ));
    }
    None
}

/// Axis determination is only measured during explicit profiling, and conventional paths do not increase timer overhead.
fn measured_axis_line(
    start: (f64, f64),
    end: (f64, f64),
    width: f64,
    bbox: [f64; 4],
    rotation: i32,
    profile: bool,
    geometry_ns: &mut u64,
) -> Option<Line> {
    if !profile {
        return axis_line(start, end, width, bbox, rotation);
    }
    let started = Instant::now();
    let result = axis_line(start, end, width, bbox, rotation);
    *geometry_ns += started.elapsed().as_nanos() as u64;
    result
}

/// Only return to the original line when all Path are strokes without matrix, no clipping, and no filling; other pages will be rolled back as a whole.
///
/// # Safety
/// The caller must verify all PDFium and ABI, hold page and library function references, and call them within the global lock.
pub unsafe fn read_fast_lines(
    addresses: Vec<usize>,
    handle: usize,
    bbox: [f64; 4],
    rotation: i32,
) -> Result<Option<Vec<Line>>, ReadError> {
    if addresses.len() != 19
        || addresses.contains(&0)
        || handle == 0
        || ![0, 90, 180, 270].contains(&rotation)
    {
        return Err(ReadError::InvalidInput(
            "invalid drawing line bridge arguments",
        ));
    }
    if !bbox.iter().all(|value| value.is_finite()) || bbox[2] <= bbox[0] || bbox[3] <= bbox[1] {
        return Ok(None);
    }
    let api = Api {
        count: std::mem::transmute::<usize, CountFn>(addresses[11]),
        get: std::mem::transmute::<usize, GetFn>(addresses[12]),
        point: std::mem::transmute::<usize, PointFn>(addresses[13]),
        kind: std::mem::transmute::<usize, SegmentFlagFn>(addresses[14]),
        closes: std::mem::transmute::<usize, SegmentFlagFn>(addresses[15]),
        draw_mode: std::mem::transmute::<usize, DrawModeFn>(addresses[16]),
        stroke_color: std::mem::transmute::<usize, StrokeColorFn>(addresses[17]),
        stroke_width: std::mem::transmute::<usize, StrokeWidthFn>(addresses[18]),
    };
    let profile = std::env::var_os("DOCVORTEX_PROFILE_DRAWING_LINES").is_some();
    let object_started = Instant::now();
    let objects = objects::read_objects(addresses[..11].to_vec(), handle, 2, 15)?;
    let object_ns = if profile {
        object_started.elapsed().as_nanos() as u64
    } else {
        0
    };
    let path_started = Instant::now();
    let mut geometry_ns = 0_u64;
    let mut lines = Vec::new();
    for (address, matrix, _parent, depth, clip) in objects {
        if depth != 0 || clip.is_some() || matrix != [1.0, 0.0, 0.0, 1.0, 0.0, 0.0] {
            return Ok(None);
        }
        let raw = address as Object;
        let (mut fill_mode, mut stroke) = (0, 0);
        if (api.draw_mode)(raw, &mut fill_mode, &mut stroke) == 0 {
            continue;
        }
        if fill_mode != 0 {
            return Ok(None);
        }
        if stroke == 0 {
            continue;
        }
        let (mut red, mut green, mut blue, mut alpha) = (0, 0, 0, 255);
        if (api.stroke_color)(raw, &mut red, &mut green, &mut blue, &mut alpha) != 0 && alpha == 0 {
            continue;
        }
        let mut raw_width: c_float = 0.0;
        let width = if (api.stroke_width)(raw, &mut raw_width) != 0 {
            let value = f64::from(raw_width).abs();
            if value.is_finite() {
                value
            } else {
                0.0
            }
        } else {
            0.0
        };
        let mut current: Option<(f64, f64)> = None;
        let mut subpath_start: Option<(f64, f64)> = None;
        for index in 0..(api.count)(raw).max(0) {
            let segment = (api.get)(raw, index);
            if segment.is_null() {
                continue;
            }
            let (mut x, mut y) = (0.0, 0.0);
            if (api.point)(segment, &mut x, &mut y) == 0 {
                continue;
            }
            let point = (f64::from(x), f64::from(y));
            let segment_type = (api.kind)(segment);
            if segment_type == 2 {
                current = Some(point);
                subpath_start = Some(point);
            } else if let Some(previous) = current {
                if segment_type == 0 {
                    if let Some(line) = measured_axis_line(
                        previous,
                        point,
                        width,
                        bbox,
                        rotation,
                        profile,
                        &mut geometry_ns,
                    ) {
                        lines.push(line);
                    }
                }
                current = Some(point);
            } else {
                current = Some(point);
                subpath_start = Some(point);
            }
            if (api.closes)(segment) != 0 {
                if let (Some(last), Some(first)) = (current, subpath_start) {
                    if last != first {
                        if let Some(line) = measured_axis_line(
                            last,
                            first,
                            width,
                            bbox,
                            rotation,
                            profile,
                            &mut geometry_ns,
                        ) {
                            lines.push(line);
                        }
                    }
                    current = Some(first);
                }
            }
        }
    }
    if profile {
        let segment_ns = (path_started.elapsed().as_nanos() as u64).saturating_sub(geometry_ns);
        *LAST_STAGE_NS
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner()) =
            (object_ns, segment_ns, geometry_ns);
    }
    Ok(Some(lines))
}
