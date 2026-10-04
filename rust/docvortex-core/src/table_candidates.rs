//! Continuous candidate calculation of table corridors: closed interval allocation, text evidence and core geometry share one preparation.
use crate::row_geometry::RowGeometry;
use std::collections::HashMap;

pub struct RuleCandidates {
    centers: Vec<f64>,
    bands: Vec<usize>,
    on_rule: Vec<bool>,
    fragment_counts: Vec<usize>,
    boxes: Vec<[f64; 4]>,
    members: Vec<usize>,
    member_offsets: Vec<usize>,
    source_ids: Vec<i64>,
    geometry: RowGeometry,
}

impl RuleCandidates {
    /// Verify and save limited pure numerical corridors at one time; horizontal lines must be strictly incremented and no rows should be deleted or rearranged.
    pub fn new(
        centers: Vec<f64>,
        rows: Vec<(f64, usize, [f64; 4], Vec<i64>)>,
    ) -> Result<Self, &'static str> {
        if centers.iter().any(|v| !v.is_finite())
            || centers.windows(2).any(|pair| pair[0] >= pair[1])
            || rows
                .iter()
                .any(|row| !row.0.is_finite() || row.2.iter().any(|v| !v.is_finite()))
        {
            return Err("nonfinite or unordered rule candidate geometry");
        }
        let mut bands = Vec::with_capacity(rows.len());
        let mut on_rule = Vec::with_capacity(rows.len());
        let mut fragment_counts = Vec::with_capacity(rows.len());
        let mut boxes = Vec::with_capacity(rows.len());
        let mut members = Vec::new();
        let mut member_offsets = Vec::with_capacity(rows.len() + 1);
        let mut source_ids = Vec::new();
        let mut interned = HashMap::new();
        member_offsets.push(0);
        for (center, count, bbox, sources) in rows {
            let band = centers.partition_point(|value| *value < center);
            bands.push(band);
            on_rule.push(centers.get(band) == Some(&center));
            fragment_counts.push(count);
            boxes.push(bbox);
            // Hash the original source only when preparing; each candidate is deduplicated by the compact ID bitmap, negative and sparse ID are not subscripted.
            for source in sources {
                let id = *interned.entry(source).or_insert_with(|| {
                    let id = source_ids.len();
                    source_ids.push(source);
                    id
                });
                members.push(id);
            }
            member_offsets.push(members.len());
        }
        let geometry = RowGeometry::new(boxes.clone()).ok_or("invalid row geometry")?;
        Ok(Self {
            centers,
            bands,
            on_rule,
            fragment_counts,
            boxes,
            members,
            member_offsets,
            source_ids,
            geometry,
        })
    }

    /// Preserve dual homing on both sides of the shared horizontal line, collect multi-unit evidence in the same line scan, and do not call Python.
    pub fn partition(
        &self,
        start: usize,
        end: usize,
        first: usize,
        rule_count: usize,
        height: f64,
    ) -> Result<(Vec<Vec<usize>>, bool), &'static str> {
        if start > end
            || end > self.bands.len()
            || first
                .checked_add(rule_count)
                .is_none_or(|last| last > self.centers.len())
            || !height.is_finite()
        {
            return Err("invalid rule candidate interval");
        }
        let count = rule_count.saturating_sub(1);
        let mut groups = vec![Vec::new(); count];
        let mut dense = vec![false; count];
        for row in start..end {
            let band = self.bands[row];
            if band > first && band - first - 1 < count {
                let local = band - first - 1;
                groups[local].push(row);
                dense[local] |= self.fragment_counts[row] >= 2;
            }
            if self.on_rule[row] && band >= first && band - first < count {
                let local = band - first;
                groups[local].push(row);
                dense[local] |= self.fragment_counts[row] >= 2;
            }
        }
        let accepted = rule_count >= 2
            && (0..count).all(|i| {
                dense[i]
                    || (i == 0
                        && !groups[i].is_empty()
                        && self.centers[first + 1] - self.centers[first] <= 2.5 * height)
            });
        Ok((groups, accepted))
    }

    /// Collect members in order of first appearance and return the source of the core union of the four coordinates, retaining the Python coordinate object with negative zero.
    pub fn core(
        &self,
        start: usize,
        end: usize,
        rules: &[[f64; 4]],
    ) -> Result<(Vec<i64>, [usize; 4]), &'static str> {
        if start >= end
            || end > self.boxes.len()
            || rules.is_empty()
            || rules.iter().flatten().any(|v| !v.is_finite())
        {
            return Err("invalid rule candidate core");
        }
        let selected = &self.members[self.member_offsets[start]..self.member_offsets[end]];
        // The bitmap only belongs to this query; immutable snapshots do not require shared tags or mutual exclusion, and will not pollute concurrent queries.
        let mut seen = vec![0_u64; self.source_ids.len().div_ceil(64)];
        let mut members = Vec::with_capacity(selected.len().min(self.source_ids.len()));
        for &id in selected {
            let word = id / 64;
            let bit = 1_u64 << (id % 64);
            if seen[word] & bit == 0 {
                seen[word] |= bit;
                members.push(self.source_ids[id]);
            }
        }
        let mut sources = [0; 4];
        for index in 1..rules.len() {
            for axis in 0..4 {
                if better(rules[index][axis], rules[sources[axis]][axis], axis) {
                    sources[axis] = index;
                }
            }
        }
        let rows = self
            .geometry
            .union_indices(start, end)
            .ok_or("empty candidate core")?;
        for axis in 0..4 {
            if better(
                self.boxes[rows[axis]][axis],
                rules[sources[axis]][axis],
                axis,
            ) {
                sources[axis] = rules.len() + rows[axis];
            }
        }
        Ok((members, sources))
    }
}

/// Strict comparison keeps the first equal value of Python min/max and does not use floating point min/max which changes the source of negative zero.
fn better(candidate: f64, current: f64, axis: usize) -> bool {
    if axis < 2 {
        candidate < current
    } else {
        candidate > current
    }
}
