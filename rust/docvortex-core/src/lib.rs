//! 不访问 Python 对象或 PDFium 的单线程批量计算内核。

pub const PROTOCOL_VERSION: u32 = 5;
pub mod columns;
pub mod spatial;

pub mod dedup;
pub mod extraction;
pub mod geometry;
pub mod scripts;
pub mod tables;

/// 对有限数值稳定排序并计算与 statistics.median 相同的中位数。
pub fn median(mut values: Vec<f64>) -> f64 {
    values.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let n = values.len();
    if n == 0 {
        return 0.0;
    }
    if n % 2 == 1 {
        values[n / 2]
    } else {
        (values[n / 2 - 1] + values[n / 2]) / 2.0
    }
}

/// 使用 Python round 的偶数舍入选择已有样本，不做插值。
pub fn quantile(mut values: Vec<f64>, fraction: f64) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    values.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    values[((values.len() - 1) as f64 * fraction).round_ties_even() as usize]
}

pub mod statistics;
