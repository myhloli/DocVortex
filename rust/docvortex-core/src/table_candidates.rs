//! 表格走廊的连续候选计算：闭区间分配、文本证据及核心几何共享一次准备。
use crate::row_geometry::RowGeometry;
use std::collections::HashSet;

pub struct RuleCandidates {
    centers: Vec<f64>,
    bands: Vec<usize>,
    on_rule: Vec<bool>,
    fragment_counts: Vec<usize>,
    boxes: Vec<[f64; 4]>,
    members: Vec<Vec<i64>>,
    geometry: RowGeometry,
}

impl RuleCandidates {
    /// 一次验证并保存有限纯数值走廊；横线必须严格递增，不删除或重排任何行。
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
        let mut members = Vec::with_capacity(rows.len());
        for (center, count, bbox, sources) in rows {
            let band = centers.partition_point(|value| *value < center);
            bands.push(band);
            on_rule.push(centers.get(band) == Some(&center));
            fragment_counts.push(count);
            boxes.push(bbox);
            members.push(sources);
        }
        let geometry = RowGeometry::new(boxes.clone()).ok_or("invalid row geometry")?;
        Ok(Self {
            centers,
            bands,
            on_rule,
            fragment_counts,
            boxes,
            members,
            geometry,
        })
    }

    /// 保留共享横线两侧的双归属，在同一行扫描中收集多单元证据，不调用 Python。
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

    /// 按首次出现顺序收集成员，并返回核心并集四个坐标的来源，保留 Python 坐标对象与负零。
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
        let mut seen = HashSet::new();
        let mut members = Vec::new();
        for row in &self.members[start..end] {
            for &source in row {
                if seen.insert(source) {
                    members.push(source);
                }
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

/// 严格比较保持 Python min/max 的首个相等值，不使用改变负零来源的浮点 min/max。
fn better(candidate: f64, current: f64, axis: usize) -> bool {
    if axis < 2 {
        candidate < current
    } else {
        candidate > current
    }
}
