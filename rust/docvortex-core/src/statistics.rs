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
