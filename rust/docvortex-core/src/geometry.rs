//! Preserve original index order by batch character box, visual line separation, and canonical geometry.

use crate::median;
pub type Box4 = [f64; 4];
pub type Size = [f64; 2];

/// Sharing risk screening and adjacent anchor statistics for the complete sample, preserving the positive width admission difference of the two paths.
pub fn anchor_pairs(
    records: Vec<(usize, Box4, Box4, Size, f64)>,
    positive_source: bool,
) -> Option<Vec<(usize, f64, f64, bool)>> {
    if records.iter().any(|r| {
        r.1.iter()
            .chain(r.2.iter())
            .chain(r.3.iter())
            .any(|v| !v.is_finite())
            || !r.4.is_finite()
    }) {
        return None;
    }
    let mut result = Vec::new();
    for (index, pair) in records.windows(2).enumerate() {
        let (a, b) = (&pair[0], &pair[1]);
        if a.0 != b.0 {
            continue;
        }
        let height = (a.2[3] - a.2[1]).max(b.2[3] - b.2[1]);
        let width = (a.2[2] - a.2[0]).max(b.2[2] - b.2[0]);
        let shift = (a.3[1] - b.3[1]).abs();
        let advance = b.3[0] - a.3[0];
        let limit = (5.0 * a.4.max(1.0)).max(8.0 * width);
        if [height, width, shift, advance, limit]
            .iter()
            .any(|v| !v.is_finite())
        {
            return None;
        }
        if shift > 0.5_f64.max(0.25 * height) || !(0.1 < advance && advance <= limit) {
            continue;
        }
        let source_width = a.1[2] - a.1[0];
        if positive_source && source_width <= 0.0 {
            continue;
        }
        let ratio = source_width / advance;
        let overlap = a.1[2] - b.2[0];
        let following_width = b.2[2] - b.2[0];
        if [ratio, overlap, following_width]
            .iter()
            .any(|v| !v.is_finite())
        {
            return None;
        }
        result.push((
            index,
            advance,
            ratio,
            overlap >= 0.05 * following_width.max(0.1),
        ));
    }
    Some(result)
}

/// Normalize and verify rectangles; strict mode does not swap endpoints, consistent with script geometry contract.
pub fn normalize(raw: Option<Box4>, strict: bool) -> Option<Box4> {
    let mut b = raw?;
    if b.iter().any(|v| !v.is_finite()) {
        return None;
    }
    if !strict {
        if b[2] < b[0] {
            b.swap(0, 2);
        }
        if b[3] < b[1] {
            b.swap(1, 3);
        }
    }
    if b[2] <= b[0] || b[3] <= b[1] {
        None
    } else {
        Some(b)
    }
}

/// Press Python min/max to retain NaN and first-term comparison semantics when equal values.
fn minimum(a: f64, b: f64) -> f64 {
    if b < a {
        b
    } else {
        a
    }
}
/// Press Python max to retain the leading signed zero.
fn maximum(a: f64, b: f64) -> f64 {
    if b > a {
        b
    } else {
        a
    }
}

/// Crop on the verified rectangle without re-swapping the endpoints.
pub fn clip(raw: Option<Box4>, size: Size) -> Option<Box4> {
    let b = raw?;
    let b = [
        maximum(0.0, minimum(size[0], b[0])),
        maximum(0.0, minimum(size[1], b[1])),
        maximum(0.0, minimum(size[0], b[2])),
        maximum(0.0, minimum(size[1], b[3])),
    ];
    if b[2] <= b[0] || b[3] <= b[1] {
        None
    } else {
        Some(b)
    }
}

/// Use the existing page visual direction transformation without using trigonometric functions or coordinate approximation.
pub fn rotate(b: Box4, size: Size, angle: i32) -> Box4 {
    match angle {
        270 => [size[1] - b[3], b[0], size[1] - b[1], b[2]],
        90 => [b[1], size[0] - b[2], b[3], size[0] - b[0]],
        180 => [
            size[0] - b[2],
            size[1] - b[3],
            size[0] - b[0],
            size[1] - b[1],
        ],
        _ => b,
    }
}

/// Convert origin and maintain the existing independent point coordinate rules.
pub fn rotate_point(p: Size, size: Size, angle: i32) -> Size {
    match angle {
        270 => [size[1] - p[1], p[0]],
        90 => [p[1], size[0] - p[0]],
        180 => [size[0] - p[0], size[1] - p[1]],
        _ => p,
    }
}

/// Find the union of finite boxes, and retain the first input for equal values.
pub fn union(boxes: &[Box4]) -> Option<Box4> {
    let mut b = *boxes.first()?;
    for a in &boxes[1..] {
        b = [
            minimum(b[0], a[0]),
            minimum(b[1], a[1]),
            maximum(b[2], a[2]),
            maximum(b[3], a[3]),
        ];
    }
    Some(b)
}

/// Compute all boxes, kerning thresholds, separations and output unions at once for visual run.
pub fn visual_runs(
    raw: Vec<Option<Box4>>,
    overrides: Vec<Option<Box4>>,
    flags: Vec<u8>,
    size: Size,
    angle: i32,
) -> Vec<(usize, usize, Option<Box4>)> {
    let boxes: Vec<_> = raw
        .into_iter()
        .zip(overrides)
        .map(|(b, o)| clip(normalize(o, false).or_else(|| normalize(b, false)), size))
        .collect();
    let local: Vec<_> = boxes
        .iter()
        .map(|b| b.map(|v| rotate(v, size, angle)))
        .collect();
    let visible: Vec<_> = (0..flags.len())
        .filter(|i| flags[*i] & 1 != 0 && local[*i].is_some())
        .collect();
    if visible.is_empty() {
        return Vec::new();
    }
    let width = median(
        visible
            .iter()
            .map(|i| 0.1_f64.max(local[*i].unwrap()[2] - local[*i].unwrap()[0]))
            .collect(),
    );
    let page_width = if angle == 90 || angle == 270 {
        size[1]
    } else {
        size[0]
    };
    let hard = 15.0_f64.max(3.0 * width).max(0.02 * page_width);
    let gaps: Vec<_> = visible
        .windows(2)
        .map(|w| {
            let a = local[w[0]].unwrap();
            let b = local[w[1]].unwrap();
            (a[0] - b[2]).max(b[0] - a[2]).max(0.0)
        })
        .collect();
    let regular: Vec<_> = gaps.iter().copied().filter(|v| *v < hard).collect();
    let regular = if regular.len() >= 3 {
        median(regular)
    } else {
        0.0
    };
    let soft = 8.0_f64.max(2.2 * width).max(3.0 * regular);
    let mut boundaries = vec![0];
    for (w, gap) in visible.windows(2).zip(gaps) {
        if gap >= hard || ((w[0] + 1..w[1]).any(|i| flags[i] & 2 != 0) && gap >= soft) {
            boundaries.push(w[1]);
        }
    }
    boundaries.push(flags.len());
    boundaries
        .windows(2)
        .map(|w| {
            let b: Vec<_> = (w[0]..w[1])
                .filter(|i| flags[*i] & 1 != 0)
                .filter_map(|i| boxes[i])
                .collect();
            (w[0], w[1], union(&b))
        })
        .collect()
}

pub type SourceRow = (Option<Box4>, Option<Box4>, Option<Box4>, Option<Size>, f64);
pub type PreparedRow = (Box4, Box4, Size, Box4, Box4, Size);

/// Implement source side-map selection, cropping and local coordinates in batches without changing the font or risk rules.
pub fn source_rows(rows: Vec<SourceRow>, size: Size, angle: i32) -> Vec<Option<PreparedRow>> {
    rows.into_iter()
        .map(|(raw, side, tight, origin, rotation)| {
            let raw = normalize(raw, false);
            let tight = normalize(tight, false);
            let source = if rotation.is_finite() && rotation.abs() <= 1e-9 {
                raw
            } else {
                let side = normalize(side, false);
                match (raw, side) {
                    (Some(r), Some(s)) => {
                        let stable = (r[2] - r[0])
                            .max(tight.map_or(0.0, |b| b[2] - b[0]))
                            .max(0.1);
                        Some(if s[2] - s[0] > 1.6 * stable { r } else { s })
                    }
                    _ => side.or(raw),
                }
            };
            let source = clip(source, size)?;
            let tight = clip(tight, size)?;
            let origin = origin.filter(|p| p.iter().all(|v| v.is_finite()))?;
            Some((
                source,
                tight,
                origin,
                rotate(source, size, angle),
                rotate(tight, size, angle),
                rotate_point(origin, size, angle),
            ))
        })
        .collect()
}
