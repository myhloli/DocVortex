//! OFD 字形矩阵、quad及裁剪；保持 Python 表达式的运算顺序和零值符号。

pub type Point = [f64; 2];
pub type BBox = [f64; 4];
pub type Matrix = [f64; 6];

#[derive(Clone, Debug)]
pub struct GlyphGeometry {
    pub bbox: BBox,
    pub quad: [Point; 4],
    pub origin: Point,
}

/// 按原六参数矩阵组合表达式逐项计算，不简化乘零或使用融合乘加。
fn compose(outer: Matrix, inner: Matrix) -> Matrix {
    let [a, b, c, d, e, f] = outer;
    let [ia, ib, ic, id, ie, if_] = inner;
    [
        a * ia + c * ib,
        b * ia + d * ib,
        a * ic + c * id,
        b * ic + d * id,
        (a * ie + c * if_) + e,
        (b * ie + d * if_) + f,
    ]
}

/// 保留两次加法的左结合顺序，包括原点变换中乘零后的符号。
fn apply(matrix: Matrix, point: Point) -> Point {
    let [a, b, c, d, e, f] = matrix;
    let [x, y] = point;
    [(a * x + c * y) + e, (b * x + d * y) + f]
}

/// 只在严格更小时替换，模拟 Python min 对相等值和正负零保留首项。
fn first_min(first: f64, second: f64) -> f64 {
    if second < first {
        second
    } else {
        first
    }
}

/// 只在严格更大时替换，模拟 Python max 对相等值和非有限裁剪值的选择。
fn first_max(first: f64, second: f64) -> f64 {
    if second > first {
        second
    } else {
        first
    }
}

/// 一次完成一字的完整矩阵组合、有限quad外接框及非退化交集。
fn glyph_geometry(
    direction: Matrix,
    rotation: Matrix,
    origin: Point,
    local: BBox,
    clip: BBox,
) -> Option<GlyphGeometry> {
    let translation = [1.0, 0.0, 0.0, 1.0, origin[0], origin[1]];
    let transform = compose(compose(direction, translation), rotation);
    let [x0, y0, x1, y1] = local;
    let quad = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]].map(|point| apply(transform, point));
    if !quad.iter().flatten().all(|value| value.is_finite()) {
        return None;
    }
    let mut bbox = [quad[0][0], quad[0][1], quad[0][0], quad[0][1]];
    for point in &quad[1..] {
        bbox[0] = first_min(bbox[0], point[0]);
        bbox[1] = first_min(bbox[1], point[1]);
        bbox[2] = first_max(bbox[2], point[0]);
        bbox[3] = first_max(bbox[3], point[1]);
    }
    if !(bbox[2] > bbox[0] && bbox[3] > bbox[1]) {
        return None;
    }
    bbox = [
        first_max(bbox[0], clip[0]),
        first_max(bbox[1], clip[1]),
        first_min(bbox[2], clip[2]),
        first_min(bbox[3], clip[3]),
    ];
    if !(bbox[2] > bbox[0] && bbox[3] > bbox[1]) {
        return None;
    }
    Some(GlyphGeometry {
        bbox,
        quad,
        origin: apply(transform, [0.0, 0.0]),
    })
}

/// 批量大小固定受限，临时数值记录不随整份文档的字形预算无限增长。
pub fn glyph_geometries(
    direction: Matrix,
    rotation: Matrix,
    origins: &[Point],
    boxes: &[BBox],
    clip: BBox,
) -> Result<Vec<Option<GlyphGeometry>>, String> {
    if origins.len() != boxes.len() || origins.len() > 256 {
        return Err("invalid OFD glyph geometry batch".into());
    }
    Ok(origins
        .iter()
        .zip(boxes)
        .map(|(&origin, &bbox)| glyph_geometry(direction, rotation, origin, bbox, clip))
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    const IDENTITY: Matrix = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0];

    /// 旋转、平移和裁剪同时作用，保留完整quad及未裁剪的原点。
    #[test]
    fn translation_rotation_and_clipping() {
        let result = glyph_geometries(
            IDENTITY,
            [0.0, 1.0, -1.0, 0.0, 0.0, 0.0],
            &[[4.0, 5.0]],
            &[[0.0, -2.0, 3.0, 1.0]],
            [0.0, 0.0, 5.0, 20.0],
        )
        .unwrap();
        let value = result[0].as_ref().unwrap();
        assert_eq!(value.bbox, [3.0, 5.0, 5.0, 8.0]);
        assert_eq!(value.quad, [[6.0, 5.0], [6.0, 8.0], [3.0, 8.0], [3.0, 5.0]]);
        assert_eq!(value.origin, [4.0, 5.0]);
    }

    /// 退化矩阵、边界相切及非有限quad都保留原来的丢弃行为。
    #[test]
    fn degenerate_and_nonfinite_geometry_is_discarded() {
        for (direction, origin, bbox, clip) in [
            (
                [0.0; 6],
                [0.0, 0.0],
                [0.0, 0.0, 1.0, 1.0],
                [-10.0, -10.0, 10.0, 10.0],
            ),
            (
                IDENTITY,
                [0.0, 0.0],
                [0.0, 0.0, 1.0, 1.0],
                [1.0, 0.0, 2.0, 1.0],
            ),
            (
                IDENTITY,
                [f64::INFINITY, 0.0],
                [0.0, 0.0, 1.0, 1.0],
                [-10.0, -10.0, 10.0, 10.0],
            ),
            (
                IDENTITY,
                [0.0, 0.0],
                [0.0, f64::NAN, 1.0, 1.0],
                [-10.0, -10.0, 10.0, 10.0],
            ),
        ] {
            assert!(
                glyph_geometries(direction, IDENTITY, &[origin], &[bbox], clip).unwrap()[0]
                    .is_none()
            );
        }
    }

    /// 比较相等时保留首个零值符号，不能替换为平台浮点min/max指令。
    #[test]
    fn extrema_keep_first_zero_sign() {
        assert_eq!(first_min(-0.0, 0.0).to_bits(), (-0.0_f64).to_bits());
        assert_eq!(first_max(-0.0, 0.0).to_bits(), (-0.0_f64).to_bits());
        assert_eq!(first_min(0.0, -0.0).to_bits(), 0.0_f64.to_bits());
        assert_eq!(first_max(0.0, -0.0).to_bits(), 0.0_f64.to_bits());
        assert_eq!(first_max(1.0, f64::NAN), 1.0);
    }

    /// 严格限制批次和并列数组长度，空批次仍可安全返回。
    #[test]
    fn batch_size_is_bounded() {
        assert!(glyph_geometries(IDENTITY, IDENTITY, &[], &[], [0.0; 4])
            .unwrap()
            .is_empty());
        assert!(glyph_geometries(IDENTITY, IDENTITY, &[[0.0; 2]], &[], [0.0; 4]).is_err());
        assert!(glyph_geometries(
            IDENTITY,
            IDENTITY,
            &[[0.0; 2]; 257],
            &[[0.0; 4]; 257],
            [0.0; 4]
        )
        .is_err());
    }
}
