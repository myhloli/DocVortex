//! OFD 字形几何边界；只传数值，在受限批次中释放GIL执行纯计算。

use pyo3::{exceptions::PyValueError, prelude::*};
use std::sync::atomic::{AtomicUsize, Ordering};

type Point = (f64, f64);
type BBox = (f64, f64, f64, f64);
type Quad = (Point, Point, Point, Point);
type GeometryRecord = Option<(BBox, Quad, Point)>;

static BATCHES: AtomicUsize = AtomicUsize::new(0);
static GLYPHS: AtomicUsize = AtomicUsize::new(0);
static VISIBLE: AtomicUsize = AtomicUsize::new(0);

/// 返回与既有GlyphItem字段相同的元组，不传字符、字体对象或任意Python回调。
#[pyfunction]
pub(super) fn ofd_glyph_geometries(
    py: Python<'_>,
    direction: [f64; 6],
    rotation: [f64; 6],
    origins: Vec<[f64; 2]>,
    boxes: Vec<[f64; 4]>,
    clip: [f64; 4],
) -> PyResult<Vec<GeometryRecord>> {
    let count = origins.len();
    let result = py
        .detach(move || {
            docvortex_core::ofd_text_geometry::glyph_geometries(
                direction, rotation, &origins, &boxes, clip,
            )
        })
        .map_err(PyValueError::new_err)?;
    BATCHES.fetch_add(1, Ordering::Relaxed);
    GLYPHS.fetch_add(count, Ordering::Relaxed);
    VISIBLE.fetch_add(
        result.iter().filter(|item| item.is_some()).count(),
        Ordering::Relaxed,
    );
    Ok(result
        .into_iter()
        .map(|item| {
            item.map(|geometry| {
                let [a, b, c, d] = geometry.bbox;
                let [p0, p1, p2, p3] = geometry.quad;
                (
                    (a, b, c, d),
                    (
                        (p0[0], p0[1]),
                        (p1[0], p1[1]),
                        (p2[0], p2[1]),
                        (p3[0], p3[1]),
                    ),
                    (geometry.origin[0], geometry.origin[1]),
                )
            })
        })
        .collect())
}

/// 报告实际完成的批次、输入字形和保留字形数量，供公开入口验收。
#[pyfunction]
pub(super) fn ofd_glyph_geometry_stats() -> (usize, usize, usize) {
    (
        BATCHES.load(Ordering::Relaxed),
        GLYPHS.load(Ordering::Relaxed),
        VISIBLE.load(Ordering::Relaxed),
    )
}
