//! 同库对象树与裁剪遍历；借用地址只允许在原页面作用域内消费。
use crate::ReadError;
use std::ffi::{c_float, c_int, c_ulong, c_void};
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
/// 借用有效页面同步读取指定类型叶子，不跨调用缓存原生地址。
///
/// # Safety
/// 调用方必须验证 11 个函数的 ABI 并持有同一 PDFium 锁、页面和运行库引用。
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
