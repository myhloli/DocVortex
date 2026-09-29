//! 同库对象树与裁剪遍历；借用地址只允许在原页面作用域内消费。
use crate::ReadError;
use std::ffi::{c_float, c_int, c_uint, c_ulong, c_void};
pub type Matrix = [f64; 6];
pub type Bounds = [f64; 4];
pub type Object = (usize, Matrix, Matrix, usize, Option<Bounds>);
#[repr(C)]
#[derive(Default)]
struct RawMatrix {
    values: [c_float; 6],
}
struct Api {
    page_count: unsafe extern "system" fn(*mut c_void) -> c_int,
    page_get: unsafe extern "system" fn(*mut c_void, c_int) -> *mut c_void,
    form_count: unsafe extern "system" fn(*mut c_void) -> c_int,
    form_get: unsafe extern "system" fn(*mut c_void, c_ulong) -> *mut c_void,
    matrix: unsafe extern "system" fn(*mut c_void, *mut RawMatrix) -> c_int,
    kind: unsafe extern "system" fn(*mut c_void) -> c_int,
    clip: unsafe extern "system" fn(*mut c_void) -> *mut c_void,
    paths: unsafe extern "system" fn(*mut c_void) -> c_int,
    segments: unsafe extern "system" fn(*mut c_void, c_int) -> c_int,
    segment: unsafe extern "system" fn(*mut c_void, c_int, c_int) -> *mut c_void,
    point: unsafe extern "system" fn(*mut c_void, *mut c_float, *mut c_float) -> c_int,
}
/// 保留 Python min 对相同值与有符号零的首项选择。
fn minimum(a: f64, b: f64) -> f64 {
    if b < a {
        b
    } else {
        a
    }
}
/// 保留 Python max 对相同值与有符号零的首项选择。
fn maximum(a: f64, b: f64) -> f64 {
    if b > a {
        b
    } else {
        a
    }
}
/// 使用与参考实现相同的运算顺序合成对象和父级矩阵。
fn multiply(a: Matrix, b: Matrix) -> Matrix {
    [
        a[0] * b[0] + a[1] * b[2],
        a[0] * b[1] + a[1] * b[3],
        a[2] * b[0] + a[3] * b[2],
        a[2] * b[1] + a[3] * b[3],
        a[4] * b[0] + a[5] * b[2] + b[4],
        a[4] * b[1] + a[5] * b[3] + b[5],
    ]
}
impl Api {
    /// 裁剪路径在父坐标中变换；损坏的局部路径不抹掉继承裁剪。
    unsafe fn clip_bounds(
        &self,
        raw: *mut c_void,
        parent: Matrix,
        inherited: Option<Bounds>,
    ) -> Option<Bounds> {
        let clip = (self.clip)(raw);
        if clip.is_null() {
            return inherited;
        }
        let mut result = inherited;
        for path in 0..(self.paths)(clip).max(0) {
            let mut bounds: Option<Bounds> = None;
            let mut finite = true;
            for index in 0..(self.segments)(clip, path).max(0) {
                let segment = (self.segment)(clip, path, index);
                let (mut x, mut y) = (0.0, 0.0);
                if (self.point)(segment, &mut x, &mut y) != 0 {
                    let px = parent[0] * x as f64 + parent[2] * y as f64 + parent[4];
                    let py = parent[1] * x as f64 + parent[3] * y as f64 + parent[5];
                    finite &= px.is_finite() && py.is_finite();
                    bounds = Some(match bounds {
                        None => [px, py, px, py],
                        Some(b) => [
                            minimum(b[0], px),
                            minimum(b[1], py),
                            maximum(b[2], px),
                            maximum(b[3], py),
                        ],
                    });
                }
            }
            if finite {
                if let Some(b) = bounds {
                    result = Some(match result {
                        None => b,
                        Some(old) => [
                            maximum(b[0], old[0]),
                            maximum(b[1], old[1]),
                            minimum(b[2], old[2]),
                            minimum(b[3], old[3]),
                        ],
                    });
                }
            }
        }
        result
    }
    /// 按原深度优先顺序遍历叶子，在 Rust 内过滤类型以减少 Python 临时对象。
    #[allow(clippy::too_many_arguments)]
    unsafe fn walk(
        &self,
        container: *mut c_void,
        form: bool,
        parent: Matrix,
        depth: usize,
        inherited: Option<Bounds>,
        max_depth: usize,
        kind: c_int,
        output: &mut Vec<Object>,
    ) {
        if depth >= max_depth {
            return;
        }
        let count = if form {
            (self.form_count)(container)
        } else {
            (self.page_count)(container)
        };
        for index in 0..count.max(0) {
            let raw = if form {
                (self.form_get)(container, index as c_ulong)
            } else {
                (self.page_get)(container, index)
            };
            if raw.is_null() {
                continue;
            }
            let mut matrix = RawMatrix::default();
            if (self.matrix)(raw, &mut matrix) == 0 {
                continue;
            }
            let combined = multiply(matrix.values.map(f64::from), parent);
            let object_kind = (self.kind)(raw);
            // 类型先于裁剪判断：类型不匹配的叶子（如纯路径页上的 Path）不必读 clip。
            if object_kind == 5 {
                let clip = self.clip_bounds(raw, parent, inherited);
                self.walk(
                    raw,
                    true,
                    combined,
                    depth + 1,
                    clip,
                    max_depth,
                    kind,
                    output,
                );
            } else if object_kind == kind {
                let clip = self.clip_bounds(raw, parent, inherited);
                output.push((raw as usize, combined, parent, depth, clip));
            }
        }
    }
}
/// TEXT 对象地址、最终可见性和页面视觉坐标中的有效裁剪框。
pub type TextVisibility = (usize, bool, Option<Bounds>);

/// 按 Python 页面坐标变换裁剪框，四个角点独立取保守外框。
fn visual_bounds(bounds: Bounds, frame: [f64; 4], rotation: i32) -> Bounds {
    let point = |x: f64, y: f64| match rotation {
        90 => (y - frame[1], x - frame[0]),
        180 => (frame[2] - x, y - frame[1]),
        270 => (frame[3] - y, frame[2] - x),
        _ => (x - frame[0], frame[3] - y),
    };
    let points = [
        point(bounds[0], bounds[1]),
        point(bounds[0], bounds[3]),
        point(bounds[2], bounds[1]),
        point(bounds[2], bounds[3]),
    ];
    let xs = [points[0].0, points[1].0, points[2].0, points[3].0];
    let ys = [points[0].1, points[1].1, points[2].1, points[3].1];
    [
        xs.iter().copied().reduce(minimum).unwrap(),
        ys.iter().copied().reduce(minimum).unwrap(),
        xs.iter().copied().reduce(maximum).unwrap(),
        ys.iter().copied().reduce(maximum).unwrap(),
    ]
}

/// 一次 TEXT 对象遍历同时计算绘制状态和有效裁剪；不跨调用保存 PDFium 地址。
///
/// # Safety
/// 调用方必须验证 14 个函数的 ABI，并持有同一 PDFium 页面、运行库和全局锁。
pub unsafe fn read_text_visibility(
    addresses: Vec<usize>,
    handle: usize,
    frame: [f64; 4],
    rotation: i32,
    max_depth: usize,
) -> Result<Vec<TextVisibility>, ReadError> {
    if addresses.len() != 14 || addresses.contains(&0) || handle == 0 {
        return Err(ReadError::InvalidInput(
            "invalid PDFium text visibility arguments",
        ));
    }
    if !frame.iter().all(|value| value.is_finite())
        || frame[2] <= frame[0]
        || frame[3] <= frame[1]
        || ![0, 90, 180, 270].contains(&rotation)
    {
        return Err(ReadError::InvalidInput(
            "invalid PDFium text visibility geometry",
        ));
    }
    let objects = read_objects(addresses[..11].to_vec(), handle, 1, max_depth)?;
    let render_mode: unsafe extern "system" fn(*mut c_void) -> c_int =
        std::mem::transmute(addresses[11]);
    let color: unsafe extern "system" fn(
        *mut c_void,
        *mut c_uint,
        *mut c_uint,
        *mut c_uint,
        *mut c_uint,
    ) -> c_int = std::mem::transmute(addresses[12]);
    let stroke_color: unsafe extern "system" fn(
        *mut c_void,
        *mut c_uint,
        *mut c_uint,
        *mut c_uint,
        *mut c_uint,
    ) -> c_int = std::mem::transmute(addresses[13]);
    let alpha = |raw: *mut c_void,
                 getter: unsafe extern "system" fn(
        *mut c_void,
        *mut c_uint,
        *mut c_uint,
        *mut c_uint,
        *mut c_uint,
    ) -> c_int|
     -> u32 {
        let (mut red, mut green, mut blue, mut alpha) = (0, 0, 0, 255);
        if getter(raw, &mut red, &mut green, &mut blue, &mut alpha) == 0 {
            return 255;
        }
        alpha
    };
    Ok(objects
        .into_iter()
        .map(|(address, _, _, _, clip)| {
            let raw = address as *mut c_void;
            let mode = render_mode(raw);
            let mut visible = mode != 3 && mode != 7;
            if (0..=2).contains(&mode) || (4..=6).contains(&mode) {
                visible = ((mode == 0 || mode == 2 || mode == 4 || mode == 6)
                    && alpha(raw, color) > 0)
                    || ((mode == 1 || mode == 2 || mode == 5 || mode == 6)
                        && alpha(raw, stroke_color) > 0);
            }
            let clip = clip.map(|value| {
                if value[2] <= value[0] || value[3] <= value[1] {
                    visible = false;
                    value
                } else {
                    visual_bounds(value, frame, rotation)
                }
            });
            (address, visible, clip)
        })
        .collect())
}

/// 借用有效页面同步读取指定类型叶子，不跨调用缓存原生地址。
///
/// # Safety
///
/// 调用方必须验证函数 ABI，并持有同一 PDFium 运行库、页面和全局锁。
pub unsafe fn read_objects(
    addresses: Vec<usize>,
    handle: usize,
    kind: c_int,
    max_depth: usize,
) -> Result<Vec<Object>, ReadError> {
    if addresses.len() != 11 || addresses.contains(&0) || handle == 0 || max_depth > 64 {
        return Err(ReadError::InvalidInput(
            "invalid PDFium object bridge arguments",
        ));
    }
    let api = Api {
        page_count: std::mem::transmute::<usize, unsafe extern "system" fn(*mut c_void) -> c_int>(
            addresses[0],
        ),
        page_get: std::mem::transmute::<
            usize,
            unsafe extern "system" fn(*mut c_void, c_int) -> *mut c_void,
        >(addresses[1]),
        form_count: std::mem::transmute::<usize, unsafe extern "system" fn(*mut c_void) -> c_int>(
            addresses[2],
        ),
        form_get: std::mem::transmute::<
            usize,
            unsafe extern "system" fn(*mut c_void, c_ulong) -> *mut c_void,
        >(addresses[3]),
        matrix: std::mem::transmute::<
            usize,
            unsafe extern "system" fn(*mut c_void, *mut RawMatrix) -> c_int,
        >(addresses[4]),
        kind: std::mem::transmute::<usize, unsafe extern "system" fn(*mut c_void) -> c_int>(
            addresses[5],
        ),
        clip: std::mem::transmute::<usize, unsafe extern "system" fn(*mut c_void) -> *mut c_void>(
            addresses[6],
        ),
        paths: std::mem::transmute::<usize, unsafe extern "system" fn(*mut c_void) -> c_int>(
            addresses[7],
        ),
        segments: std::mem::transmute::<
            usize,
            unsafe extern "system" fn(*mut c_void, c_int) -> c_int,
        >(addresses[8]),
        segment: std::mem::transmute::<
            usize,
            unsafe extern "system" fn(*mut c_void, c_int, c_int) -> *mut c_void,
        >(addresses[9]),
        point: std::mem::transmute::<
            usize,
            unsafe extern "system" fn(*mut c_void, *mut c_float, *mut c_float) -> c_int,
        >(addresses[10]),
    };
    let mut output = Vec::new();
    api.walk(
        handle as *mut c_void,
        false,
        [1.0, 0.0, 0.0, 1.0, 0.0, 0.0],
        0,
        None,
        max_depth,
        kind,
        &mut output,
    );
    Ok(output)
}
