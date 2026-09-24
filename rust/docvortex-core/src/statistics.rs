//! 对单调追加的聚类直接读取中位位置，保持原比较顺序和成员索引。

/// 输入已稳定排序；每个簇按全局顺序追加，不需要再次排序。
fn ordered_median(indices: &[usize], values: &[f64]) -> f64 {
    let n = indices.len();
    if n % 2 == 1 {
        values[indices[n / 2]]
    } else {
        (values[indices[n / 2 - 1]] + values[indices[n / 2]]) / 2.0
    }
}

/// 按绝对或相对容差选首个簇，last_only 保留只检查末簇的原策略。
pub fn ordered_clusters(
    values: Vec<f64>,
    tolerance: f64,
    relative: f64,
    last_only: bool,
) -> Option<Vec<Vec<usize>>> {
    if values.iter().any(|v| !v.is_finite()) || !tolerance.is_finite() || !relative.is_finite() {
        return None;
    }
    let mut order: Vec<_> = (0..values.len()).collect();
    order.sort_by(|a, b| values[*a].partial_cmp(&values[*b]).unwrap());
    let mut clusters: Vec<Vec<usize>> = Vec::new();
    for index in order {
        let start = if last_only {
            clusters.len().saturating_sub(1)
        } else {
            0
        };
        let target = (start..clusters.len()).find(|&i| {
            let middle = ordered_median(&clusters[i], &values);
            let limit = if relative == 0.0 {
                tolerance
            } else {
                relative * middle
            };
            (values[index] - middle).abs() <= limit
        });
        if let Some(target) = target {
            clusters[target].push(index);
        } else {
            clusters.push(vec![index]);
        }
    }
    Some(clusters)
}

use crate::geometry::Box4;
use crate::median;

pub type Typography = (
    f64,
    Option<f64>,
    Option<usize>,
    f64,
    Option<f64>,
    Option<f64>,
    Option<f64>,
);

/// 一次计算整行字体与字形统计；类别编号由 Python 按其字符串/整数语义提供。
pub fn typography(
    boxes: Vec<Option<Box4>>,
    fonts: Vec<Option<usize>>,
    weights: Vec<Option<f64>>,
    families: Vec<Option<usize>>,
    fallback_height: f64,
) -> Option<Typography> {
    if boxes.len() != fonts.len()
        || boxes.len() != weights.len()
        || fonts.iter().flatten().any(|i| *i >= families.len())
        || !fallback_height.is_finite()
        || boxes.iter().flatten().flatten().any(|v| !v.is_finite())
        || weights.iter().flatten().any(|v| !v.is_finite())
    {
        return None;
    }
    let mut heights = Vec::new();
    let mut widths = Vec::new();
    let mut counts = vec![0usize; families.len()];
    let mut font_weights = vec![Vec::new(); families.len()];
    let mut seen = Vec::new();
    let mut glyphs = Vec::new();
    for ((raw, font), weight) in boxes.into_iter().zip(fonts).zip(weights) {
        let Some(b) = raw else {
            continue;
        };
        heights.push(0.1_f64.max(b[3] - b[1]));
        widths.push(0.1_f64.max(b[2] - b[0]));
        if let Some(id) = font {
            if counts[id] == 0 {
                seen.push(id);
            }
            counts[id] += 1;
            if let Some(weight) = weight {
                font_weights[id].push(weight);
            }
        }
        glyphs.push((b, font, weight));
    }
    let height = if heights.is_empty() {
        fallback_height
    } else {
        median(heights)
    };
    let width = if widths.is_empty() {
        None
    } else {
        Some(median(widths))
    };
    let mut winner = None;
    for id in seen {
        if winner.is_none_or(|previous| counts[id] > counts[previous]) {
            winner = Some(id);
        }
    }
    let coverage = winner.map_or(0.0, |id| {
        counts[id] as f64 / counts.iter().sum::<usize>() as f64
    });
    let weight = winner.and_then(|id| {
        if font_weights[id].is_empty() {
            None
        } else {
            Some(median(font_weights[id].clone()))
        }
    });
    let mut emphasis = None;
    let mut typography_width = None;
    if glyphs.len() >= 4 {
        if let Some(first) = glyphs[0].1 {
            let split = glyphs
                .iter()
                .position(|g| g.1 != Some(first))
                .unwrap_or(glyphs.len());
            if split >= 2 && glyphs.len() - split >= 2 {
                let left = glyphs[..split]
                    .iter()
                    .map(|g| g.0[0])
                    .fold(f64::INFINITY, f64::min);
                let right = glyphs[..split]
                    .iter()
                    .map(|g| g.0[2])
                    .fold(f64::NEG_INFINITY, f64::max);
                let prefix_width = 0.1_f64.max(right - left);
                let prefix: Vec<_> = glyphs[..split].iter().filter_map(|g| g.2).collect();
                let body: Vec<_> = glyphs[split..].iter().filter_map(|g| g.2).collect();
                if !prefix.is_empty() && !body.is_empty() {
                    let p = median(prefix);
                    let b = median(body);
                    if p - b >= 100.0 && p >= 1.15 * 1.0_f64.max(b) {
                        emphasis = Some(prefix_width);
                    }
                }
                if let Some(first_family) = families[first] {
                    let mut body_counts = vec![0; families.len()];
                    let mut body_seen = Vec::new();
                    for (_, id, _) in &glyphs[split..] {
                        if let Some(id) = *id {
                            if body_counts[id] == 0 {
                                body_seen.push(id);
                            }
                            body_counts[id] += 1;
                        }
                    }
                    let mut body_winner = None;
                    for id in body_seen {
                        if body_winner
                            .is_none_or(|previous| body_counts[id] > body_counts[previous])
                        {
                            body_winner = Some(id);
                        }
                    }
                    if let Some(id) = body_winner {
                        if body_counts[id] >= 2 && families[id] != Some(first_family) {
                            typography_width = Some(prefix_width);
                        }
                    }
                }
            }
        }
    }
    Some((
        height,
        width,
        winner,
        coverage,
        weight,
        emphasis,
        typography_width,
    ))
}

/// 输入沿 Python 的稳定成员排序；快照只在本次调用使用，不缓存可变行。
pub fn lane_gap(
    boxes: Vec<Box4>,
    heights: Vec<f64>,
    restored: Vec<bool>,
    skip: Vec<bool>,
) -> Option<(f64, f64)> {
    let n = boxes.len();
    if heights.len() != n
        || restored.len() != n
        || skip.len() != n.saturating_sub(1)
        || boxes.iter().flatten().any(|v| !v.is_finite())
        || heights.iter().any(|v| !v.is_finite() || *v <= 0.0)
    {
        return None;
    }
    let mh = if n == 0 { 1.0 } else { median(heights.clone()) };
    let mut gaps = Vec::new();
    for i in 1..n {
        if skip[i - 1] {
            continue;
        }
        let a = boxes[i - 1];
        let b = boxes[i];
        let ah = heights[i - 1];
        let bh = heights[i];
        if ah.max(bh) / ah.min(bh) > 1.35 {
            continue;
        }
        let h = ah.max(bh);
        let gap = if restored[i - 1] {
            (b[1] - a[3]).max(-0.25 * ah)
        } else {
            b[1] - (a[1] + ah)
        };
        if gap < -0.25 * h || gap > 2.0 * h {
            continue;
        }
        let overlap = 0.0_f64.max(a[2].min(b[2]) - a[0].max(b[0]));
        let shorter = (a[2] - a[0]).min(b[2] - b[0]);
        let ratio = if shorter > 0.0 {
            overlap / shorter
        } else {
            0.0
        };
        if ratio < 0.5 && (a[0] - b[0]).abs() > 1.5 * mh {
            continue;
        }
        gaps.push(0.0_f64.max(gap));
    }
    if gaps.is_empty() {
        return Some((0.35 * mh, 0.0));
    }
    gaps.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let count = 1usize.max((gaps.len() as f64 * 0.6).ceil() as usize);
    let lower = gaps[..count].to_vec();
    let regular = median(lower.clone());
    let mad = median(lower.into_iter().map(|g| (g - regular).abs()).collect());
    Some((regular, mad))
}
