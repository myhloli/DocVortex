//! Single-threaded batch computing kernel without access to Python object or PDFium.

pub const PROTOCOL_VERSION: u32 = 30;
pub mod columns;
pub mod note_index;
pub mod row_geometry;
pub mod spatial;
pub mod text_classification;

pub mod dedup;
pub mod extraction;
pub mod geometry;
pub mod scripts;
pub mod tables;

/// Stably sort finite values and calculate the same median as statistics.median.
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

/// Use the even rounding of Python round to select existing samples without interpolation.
pub fn quantile(mut values: Vec<f64>, fraction: f64) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    values.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    values[((values.len() - 1) as f64 * fraction).round_ties_even() as usize]
}

pub mod statistics;

pub mod inline_pairs;

pub mod annotation_geometry;
pub mod table_candidates;
pub mod text_pipeline;

pub mod text_assignment;
pub mod text_content;
pub mod text_snapshot;
pub mod text_spacing;

pub mod geometry_risk;
pub mod geometry_runs;
pub mod geometry_style;

pub mod geometry_lines;

pub mod table_merge;

pub mod pixels;

pub mod inline_styles;
