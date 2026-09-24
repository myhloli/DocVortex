//! 有界同行区间查询；每次只返回一小批行，不保存平方规模的配对集合。

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
    /// 建立按左端点排序的最大右端点树，输入必须是已经验证的有限区间。
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

    /// 返回当前行右侧的相交行，按原始索引排序以保持并查集合并顺序。
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

    /// 批量查询有限行数；单个极密行可以超过预算，但不会缓存整个页面的行对。
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
