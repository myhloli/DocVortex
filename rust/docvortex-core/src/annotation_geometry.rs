//! Filter and aggregate table-annotation fragments in source order, returning original coordinate sources without allocating floats.
use crate::geometry::Box4;
use std::collections::{HashMap, HashSet};

pub type Fragment = (usize, Box4, Box4);
pub type Output = (Vec<(usize, [usize; 4], usize)>, Option<[usize; 4]>);

pub struct AnnotationGeometry {
    fragments: Vec<Fragment>,
}

impl AnnotationGeometry {
    /// Store read-only corridor fragments and bound finite coordinate magnitudes to keep intermediate arithmetic safe.
    pub fn new(fragments: Vec<Fragment>) -> Option<Self> {
        if fragments.iter().any(|f| {
            f.1.iter()
                .chain(f.2.iter())
                .any(|v| !v.is_finite() || v.abs() > 1e100)
        }) {
            return None;
        }
        Some(Self { fragments })
    }

    /// Use strict comparisons to preserve the first coordinate source, including equal values and signed zero.
    fn combine(&self, a: [usize; 4], b: [usize; 4]) -> [usize; 4] {
        let mut result = a;
        for k in 0..4 {
            let av = self.fragments[a[k]].1[k];
            let bv = self.fragments[b[k]].1[k];
            if (k < 2 && bv < av) || (k >= 2 && bv > av) {
                result[k] = b[k];
            }
        }
        result
    }

    /// Process exclusions and duplicate sources in the candidate's original fragment order; explicitly reject invalid indices.
    pub fn aggregate(
        &self,
        selected: Vec<usize>,
        excluded: Vec<usize>,
        local: Option<Box4>,
    ) -> Option<Output> {
        if selected.iter().any(|i| *i >= self.fragments.len())
            || local.is_some_and(|b| b.iter().any(|v| !v.is_finite() || v.abs() > 1e100))
        {
            return None;
        }
        let excluded: HashSet<_> = excluded.into_iter().collect();
        let mut positions = HashMap::new();
        let mut lines: Vec<(usize, [usize; 4], usize)> = Vec::new();
        for i in selected {
            let (source, _, b) = self.fragments[i];
            if excluded.contains(&source) {
                continue;
            }
            if let Some(region) = local {
                let smaller = (b[2] - b[0]).min(region[2] - region[0]);
                let overlap = 0.0_f64.max(b[2].min(region[2]) - b[0].max(region[0]));
                let ratio = if smaller > 0.0 {
                    overlap / smaller
                } else {
                    0.0
                };
                if b[3] > region[1] && ratio >= 0.05 {
                    continue;
                }
            }
            if let Some(&position) = positions.get(&source) {
                let line: &mut (usize, [usize; 4], usize) = &mut lines[position];
                line.1 = self.combine(line.1, [i; 4]);
                line.2 += 1;
            } else {
                positions.insert(source, lines.len());
                lines.push((source, [i; 4], 1));
            }
        }
        let union = lines.iter().map(|v| v.1).reduce(|a, b| self.combine(a, b));
        Some((lines, union))
    }
}
