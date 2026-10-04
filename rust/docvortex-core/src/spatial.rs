//! Bounded peer interval query; only a small batch of rows are returned each time, and square-scale paired sets are not saved.

use crate::geometry::Box4;

/// Reproduce the horizontal overlap ratio of the finite box, and the zero-width and reverse boxes maintain the original zero support.
fn x_overlap(a: Box4, b: Box4) -> f64 {
    let length = a[2].min(b[2]) - a[0].max(b[0]);
    let smaller = (a[2] - a[0]).min(b[2] - b[0]);
    if smaller > 0.0 {
        (if length > 0.0 { length } else { 0.0 }) / smaller
    } else {
        0.0
    }
}

/// Header headroom is calculated in batches, identical objects and visual rows are processed in source order, and overflows are handed over to Python.
pub fn title_gaps(
    records: Vec<(usize, Option<usize>, Box4, f64, bool)>,
) -> Option<Vec<(Option<f64>, Option<f64>)>> {
    if records.iter().any(|r| {
        r.2.iter().any(|v| !v.is_finite())
            || !r.3.is_finite()
            || !(r.2[2] - r.2[0]).is_finite()
            || !(r.2[1] + r.2[3]).is_finite()
    }) {
        return None;
    }
    let centers: Vec<_> = records.iter().map(|r| (r.2[1] + r.2[3]) / 2.0).collect();
    let mut output = Vec::with_capacity(records.len());
    for (i, a) in records.iter().enumerate() {
        let (mut above, mut below): (Option<f64>, Option<f64>) = (None, None);
        for (j, b) in records.iter().enumerate() {
            if a.0 == b.0 || x_overlap(a.2, b.2) < 0.1 || (a.1.is_some() && a.1 == b.1) {
                continue;
            }
            let (first, second, target) = if centers[j] < centers[i] {
                (b, a, &mut above)
            } else if centers[j] > centers[i] {
                (a, b, &mut below)
            } else {
                continue;
            };
            let gap = if first.4 {
                (second.2[1] - first.2[3]).max(-0.25 * first.3)
            } else {
                second.2[1] - (first.2[1] + first.3)
            };
            if !gap.is_finite() {
                return None;
            }
            let gap = if gap > 0.0 { gap } else { 0.0 };
            if target.is_none_or(|previous| gap < previous) {
                *target = Some(gap);
            }
        }
        output.push((above, below));
    }
    Some(output)
}

/// Select the nearest upper and lower neighbor rows for a page of geometric analysis; keep the first source if the distance is equal, and return the index instead of copying the object.
pub fn line_neighbors(
    records: Vec<(usize, Box4, f64, f64)>,
) -> Option<Vec<(Option<usize>, Option<usize>)>> {
    if records.iter().any(|r| {
        r.1.iter().any(|v| !v.is_finite())
            || !r.2.is_finite()
            || !r.3.is_finite()
            || !(r.1[2] - r.1[0]).is_finite()
    }) {
        return None;
    }
    let mut output = Vec::with_capacity(records.len());
    for a in &records {
        let (mut above, mut below): (Option<usize>, Option<usize>) = (None, None);
        for (j, b) in records.iter().enumerate() {
            if a.0 == b.0 {
                continue;
            }
            let height = a.2.max(b.2);
            let shift = (b.3 - a.3).abs();
            if !shift.is_finite() || !(2.0 * height.max(1.0)).is_finite() {
                return None;
            }
            if !(x_overlap(a.1, b.1) >= 0.35 || (a.1[0] - b.1[0]).abs() <= 2.0 * height.max(1.0))
                || shift < 2.0_f64.max(0.6 * a.2)
            {
                continue;
            }
            if b.3 < a.3 && above.is_none_or(|k| b.3 > records[k].3) {
                above = Some(j);
            }
            if b.3 > a.3 && below.is_none_or(|k| b.3 < records[k].3) {
                below = Some(j);
            }
        }
        output.push((above, below));
    }
    Some(output)
}

#[derive(Debug)]
struct Group {
    order: Vec<usize>,
    lows: Vec<f64>,
    maxima: Vec<f64>,
    width: usize,
}

#[derive(Debug)]
pub struct IntervalIndex {
    bounds: Vec<(f64, f64)>,
    group_ids: Vec<usize>,
    groups: Vec<Group>,
}

impl IntervalIndex {
    /// To establish the maximum right endpoint tree sorted by the left endpoint, the input must be a verified limited interval.
    pub fn new(bounds: Vec<(f64, f64)>, group_ids: Vec<usize>) -> Option<Self> {
        if bounds.len() != group_ids.len()
            || bounds
                .iter()
                .any(|(a, b)| !a.is_finite() || !b.is_finite() || a > b)
            || group_ids.iter().any(|g| *g >= bounds.len())
        {
            return None;
        }
        let count = group_ids.iter().max().map_or(0, |g| g + 1);
        let mut members = vec![Vec::new(); count];
        for (i, &group) in group_ids.iter().enumerate() {
            members[group].push(i);
        }
        let mut groups = Vec::with_capacity(count);
        for mut order in members {
            order.sort_by(|a, b| {
                bounds[*a]
                    .0
                    .partial_cmp(&bounds[*b].0)
                    .unwrap()
                    .then(a.cmp(b))
            });
            let width = order.len().max(1).next_power_of_two();
            let mut maxima = vec![f64::NEG_INFINITY; 2 * width];
            for (position, &index) in order.iter().enumerate() {
                maxima[width + position] = bounds[index].1;
            }
            for node in (1..width).rev() {
                maxima[node] = maxima[2 * node].max(maxima[2 * node + 1]);
            }
            let lows = order.iter().map(|i| bounds[*i].0).collect();
            groups.push(Group {
                order,
                lows,
                maxima,
                width,
            });
        }
        Some(Self {
            bounds,
            group_ids,
            groups,
        })
    }

    /// Return the intersecting rows to the right of the current row, sorted by the original index to maintain the merge order of the merge lookup.
    pub fn query(&self, index: usize) -> Vec<usize> {
        let (low, high) = self.bounds[index];
        let group = &self.groups[self.group_ids[index]];
        let end = group.lows.partition_point(|value| *value <= high);
        let mut stack = vec![(1, 0, group.width)];
        let mut result = Vec::new();
        while let Some((node, left, right)) = stack.pop() {
            if left >= end || group.maxima[node] < low {
                continue;
            }
            if right - left == 1 {
                let other = group.order[left];
                if other > index {
                    result.push(other);
                }
            } else {
                let middle = (left + right) / 2;
                stack.push((2 * node + 1, middle, right));
                stack.push((2 * node, left, middle));
            }
        }
        result.sort_unstable();
        result
    }

    /// Batch queries have a limited number of rows; a single extremely dense row can exceed the budget, but an entire page of row pairs will not be cached.
    pub fn rows(&self, start: usize, count: usize, budget: usize) -> Vec<Vec<usize>> {
        let mut result = Vec::new();
        let mut total = 0;
        for index in start..start.saturating_add(count).min(self.bounds.len()) {
            let row = self.query(index);
            total += row.len();
            result.push(row);
            if total >= budget {
                break;
            }
        }
        result
    }
}

/// Peer safe superset filtering, retaining two independent paths of ordinary boxes and original continuous character boxes.
pub struct BaselineGeometry {
    index: IntervalIndex,
    boxes: Vec<Box4>,
    heights: Vec<f64>,
    sources: Vec<Option<Box4>>,
}

/// The reference peer geometry of the finite forward frame, the calculation sequence is consistent with Python.
fn same_baseline(a: Box4, ah: f64, b: Box4, bh: f64) -> bool {
    let h = ah.max(bh);
    if ah.min(bh) <= 0.0 || h / ah.min(bh) > 1.35 {
        return false;
    }
    let overlap = 0.0_f64.max(a[3].min(b[3]) - a[1].max(b[1]));
    let shorter = 0.1_f64.max((a[3] - a[1]).min(b[3] - b[1]));
    if overlap / shorter < 0.7 && (a[3] - b[3]).abs() > 0.25 * h {
        return false;
    }
    let (left, right) = if a[0] <= b[0] { (a, b) } else { (b, a) };
    let gap = right[0] - left[2];
    gap >= -0.25 * h && gap <= 3.0_f64.max(0.75 * h)
}

/// Verify the packaging record and limit the range to avoid intermediate operation overflow and change necessary conditions.
fn safe_box(b: &Box4) -> bool {
    b.iter().all(|v| v.is_finite() && v.abs() <= 1e100) && b[2] > b[0] && b[3] > b[1]
}

impl BaselineGeometry {
    /// Each closure phase builds an independent read-only index and does not reuse the old geometry that has been merged.
    pub fn new(
        bounds: Vec<(f64, f64)>,
        groups: Vec<usize>,
        boxes: Vec<Box4>,
        heights: Vec<f64>,
        sources: Vec<Option<Box4>>,
    ) -> Option<Self> {
        let n = bounds.len();
        if boxes.len() != n
            || heights.len() != n
            || sources.len() != n
            || boxes.iter().any(|b| !safe_box(b))
            || heights.iter().any(|h| !h.is_finite() || h.abs() > 1e100)
            || sources.iter().flatten().any(|b| !safe_box(b))
        {
            return None;
        }
        Some(Self {
            index: IntervalIndex::new(bounds, groups)?,
            boxes,
            heights,
            sources,
        })
    }

    /// Only row pairs that are impossible to accept in all reference branches are eliminated, and semantic and table judgments are still performed by Python.
    fn accepts(&self, i: usize, j: usize) -> bool {
        let (a, b, ah, bh) = (
            self.boxes[i],
            self.boxes[j],
            self.heights[i],
            self.heights[j],
        );
        if same_baseline(a, ah, b, bh) {
            return true;
        }
        if let (Some(sa), Some(sb)) = (self.sources[i], self.sources[j]) {
            if same_baseline(sa, sa[3] - sa[1], sb, sb[3] - sb[1]) {
                return true;
            }
        }
        let h = ah.max(bh);
        let overlap = 0.0_f64.max(a[3].min(b[3]) - a[1].max(b[1]));
        let smaller = 0.1_f64.max((a[3] - a[1]).min(b[3] - b[1]));
        if overlap / smaller < 0.7 || ((a[1] + a[3]) / 2.0 - (b[1] + b[3]) / 2.0).abs() > 0.5 * h {
            return false;
        }
        let (left, right) = if a[0] <= b[0] { (a, b) } else { (b, a) };
        let gap = right[0] - left[2];
        gap >= -0.15 * h && gap <= 0.75
    }

    /// The bounded row batch is returned in order, and all members are still returned when a single row exceeds the budget.
    pub fn rows(&self, start: usize, count: usize, budget: usize) -> Vec<Vec<usize>> {
        let mut output = Vec::new();
        let mut total = 0;
        for i in start..start.saturating_add(count).min(self.boxes.len()) {
            let row: Vec<_> = self
                .index
                .query(i)
                .into_iter()
                .filter(|j| self.accepts(i, *j))
                .collect();
            total += row.len();
            output.push(row);
            if total >= budget {
                break;
            }
        }
        output
    }
}
