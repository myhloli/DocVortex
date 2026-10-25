//! OFD 线段连通分量与基础格拓扑；索引减少实际计算，虚拟比较预算保持原计费。

#[derive(Clone, Copy, Debug)]
pub struct AxisLine {
    pub horizontal: bool,
    pub coordinate: f64,
    pub start: f64,
    pub end: f64,
}

#[derive(Clone, Copy, Debug)]
pub struct Budget {
    pub count: i64,
    pub limit: i64,
}

impl Budget {
    /// 校验整数传输边界，确保第一次超限计数仍可精确返回。
    pub fn new(count: i64, limit: i64) -> Result<Self, String> {
        if count < 0 || limit < 0 || count == i64::MAX || limit == i64::MAX {
            return Err("invalid OFD comparison budget".into());
        }
        Ok(Self { count, limit })
    }

    /// 批量计入原本逐项发生的比较，超限只增长到原始第一次失败的位置。
    fn charge(&mut self, comparisons: usize) -> bool {
        if comparisons == 0 {
            return true;
        }
        if self.count >= self.limit {
            self.count += 1;
            return false;
        }
        let remaining = (self.limit - self.count) as u64;
        if comparisons as u64 > remaining {
            self.count = self.limit + 1;
            false
        } else {
            self.count += comparisons as i64;
            true
        }
    }
}

#[derive(Debug)]
pub struct Outcome<T> {
    pub value: Option<T>,
    pub budget_count: i64,
    pub exceeded: bool,
    pub actual_checks: usize,
}

/// 返回完整结果或资源错误，并保留本次实际候选检查次数。
fn outcome<T>(value: Option<T>, budget: Budget, exceeded: bool, checks: usize) -> Outcome<T> {
    Outcome {
        value,
        budget_count: budget.count,
        exceeded,
        actual_checks: checks,
    }
}

/// 限定受支持的有限有序线段，公开 Python 的其它输入仍由原路径解释。
fn validate_lines(lines: &[AxisLine]) -> Result<(), String> {
    if lines.len() > 2_000
        || lines.iter().any(|line| {
            !line.coordinate.is_finite()
                || !line.start.is_finite()
                || !line.end.is_finite()
                || line.start > line.end
        })
    {
        return Err("invalid OFD axis snapshot".into());
    }
    Ok(())
}

struct AxisIndex {
    horizontal: Vec<usize>,
    vertical: Vec<usize>,
}

impl AxisIndex {
    /// 按方向分列，再按坐标稳定排序，不改变原线段身份或输出成员顺序。
    fn new(lines: &[AxisLine]) -> Self {
        let mut horizontal = Vec::new();
        let mut vertical = Vec::new();
        for (index, line) in lines.iter().enumerate() {
            if line.horizontal {
                horizontal.push(index);
            } else {
                vertical.push(index);
            }
        }
        for indices in [&mut horizontal, &mut vertical] {
            indices.sort_by(|&first, &second| {
                lines[first]
                    .coordinate
                    .partial_cmp(&lines[second].coordinate)
                    .unwrap()
            });
        }
        Self {
            horizontal,
            vertical,
        }
    }

    /// 返回保守坐标区间，最终谓词继续使用原始浮点操作与容差。
    fn range<'a>(
        &'a self,
        lines: &[AxisLine],
        horizontal: bool,
        low: f64,
        high: f64,
    ) -> &'a [usize] {
        let indices = if horizontal {
            &self.horizontal
        } else {
            &self.vertical
        };
        let low = low.next_down();
        let high = high.next_up();
        if low > high {
            return &indices[0..0];
        }
        let start = indices.partition_point(|&index| lines[index].coordinate < low);
        let end = indices.partition_point(|&index| lines[index].coordinate <= high);
        &indices[start..end]
    }
}

/// 使用与原实现相同的中心坐标、减法顺序及相交容差判定连接。
fn touches(first: AxisLine, second: AxisLine) -> bool {
    if first.horizontal == second.horizontal {
        if (first.coordinate - second.coordinate).abs() > 0.6 {
            return false;
        }
        first.end.min(second.end) + 0.8 >= first.start.max(second.start)
    } else {
        let (horizontal, vertical) = if first.horizontal {
            (first, second)
        } else {
            (second, first)
        };
        horizontal.start - 0.8 <= vertical.coordinate
            && vertical.coordinate <= horizontal.end + 0.8
            && vertical.start - 0.8 <= horizontal.coordinate
            && horizontal.coordinate <= vertical.end + 0.8
    }
}

/// 查找并压缩索引根，保留整数成员的稳定身份。
fn root(parents: &mut [usize], mut index: usize) -> usize {
    while parents[index] != index {
        parents[index] = parents[parents[index]];
        index = parents[index];
    }
    index
}

/// 用方向/坐标索引寻找真实连接，原两两比较数量仍完整计入文档预算。
pub fn components(
    lines: &[AxisLine],
    mut budget: Budget,
) -> Result<Outcome<Vec<Vec<usize>>>, String> {
    validate_lines(lines)?;
    let count = lines.len();
    if !budget.charge(count * count.saturating_sub(1) / 2) {
        return Ok(outcome(None, budget, true, 0));
    }
    let index = AxisIndex::new(lines);
    let mut parents: Vec<usize> = (0..count).collect();
    let mut checks = 0;
    // 先扩大容差自身一ULP，覆盖差值舍入到0.6而端点加法发生抵消的情况。
    let coordinate_radius = 0.6_f64.next_up();
    for (first_index, &first) in lines.iter().enumerate() {
        let same = index.range(
            lines,
            first.horizontal,
            first.coordinate - coordinate_radius,
            first.coordinate + coordinate_radius,
        );
        // 异向连接只从水平线查询垂直轨道，避免每对线重复进入实际谓词。
        let cross = if first.horizontal {
            index.range(lines, false, first.start - 0.8, first.end + 0.8)
        } else {
            &[]
        };
        for (&second_index, same_direction) in same
            .iter()
            .map(|value| (value, true))
            .chain(cross.iter().map(|value| (value, false)))
        {
            if same_direction && second_index <= first_index {
                continue;
            }
            checks += 1;
            if touches(first, lines[second_index]) {
                let first_root = root(&mut parents, first_index);
                let second_root = root(&mut parents, second_index);
                parents[first_root.max(second_root)] = first_root.min(second_root);
            }
        }
    }
    let mut groups = vec![Vec::new(); count];
    for line in 0..count {
        let group = root(&mut parents, line);
        groups[group].push(line);
    }
    Ok(outcome(
        Some(
            groups
                .into_iter()
                .filter(|group| !group.is_empty())
                .collect(),
        ),
        budget,
        false,
        checks,
    ))
}

/// 扫描已排序的共线区间，保留覆盖判定的提前返回和末尾容差。
fn covers(intervals: &[(f64, f64)], start: f64, end: f64) -> bool {
    let mut cursor = start;
    for &(left, right) in intervals {
        if right < cursor - 0.8 {
            continue;
        }
        if left > cursor + 0.8 {
            return false;
        }
        if right > cursor {
            cursor = right;
        }
        if cursor >= end - 0.8 {
            return true;
        }
    }
    cursor >= end - 0.8
}

/// 只收集容差内的同向线段，区间按 Python 数值元组顺序稳定排序。
fn aligned_intervals(
    lines: &[AxisLine],
    index: &AxisIndex,
    horizontal: bool,
    coordinate: f64,
) -> Vec<(f64, f64)> {
    // 不能只对相加后的端点外扩一ULP：接近零的端点ULP远小于减法舍入误差。
    let coordinate_radius = 0.6_f64.next_up();
    let mut intervals: Vec<_> = index
        .range(
            lines,
            horizontal,
            coordinate - coordinate_radius,
            coordinate + coordinate_radius,
        )
        .iter()
        .map(|&index| lines[index])
        .filter(|line| (line.coordinate - coordinate).abs() <= 0.6)
        .map(|line| (line.start, line.end))
        .collect();
    intervals.sort_by(|first, second| {
        first
            .0
            .partial_cmp(&second.0)
            .unwrap()
            .then_with(|| first.1.partial_cmp(&second.1).unwrap())
    });
    intervals
}

/// 判定完整边、局部边或缺失边；缺失边在短区间上也保留原覆盖语义。
fn boundary_state(intervals: &[(f64, f64)], start: f64, end: f64) -> i8 {
    if covers(intervals, start, end) {
        1
    } else if intervals
        .iter()
        .any(|&(left, right)| right.min(end) - left.max(start) > 0.8)
    {
        -1
    } else {
        0
    }
}

/// 验证有限且递增的网格，保持分配仅与受限基础格数量有关。
fn validate_axes(xs: &[f64], ys: &[f64]) -> Result<(), String> {
    if xs.len() < 2
        || ys.len() < 2
        || (xs.len() - 1)
            .checked_mul(ys.len() - 1)
            .is_none_or(|count| count > 10_000)
        || [xs, ys].iter().any(|axis| {
            axis.iter().any(|value| !value.is_finite())
                || axis.windows(2).any(|pair| pair[0] >= pair[1])
        })
    {
        return Err("invalid OFD grid snapshot".into());
    }
    Ok(())
}

/// 按原右邻、下邻顺序连通基础格，索引轨道区间可在同一网格内复用。
pub fn logical_cells(
    xs: &[f64],
    ys: &[f64],
    lines: &[AxisLine],
    mut budget: Budget,
) -> Result<Outcome<Vec<[usize; 4]>>, String> {
    validate_lines(lines)?;
    validate_axes(xs, ys)?;
    let rows = ys.len() - 1;
    let columns = xs.len() - 1;
    let index = AxisIndex::new(lines);
    let mut vertical = vec![None; xs.len()];
    let mut horizontal = vec![None; ys.len()];
    let mut parents: Vec<usize> = (0..rows * columns).collect();
    let mut present_edges = Vec::new();
    let mut checks = 0;
    for row in 0..rows {
        for column in 0..columns {
            let current = row * columns + column;
            for right in [true, false] {
                let (neighbor, intervals, coordinate, start, end) = if right && column + 1 < columns
                {
                    (
                        current + 1,
                        &mut vertical[column + 1],
                        xs[column + 1],
                        ys[row],
                        ys[row + 1],
                    )
                } else if !right && row + 1 < rows {
                    (
                        current + columns,
                        &mut horizontal[row + 1],
                        ys[row + 1],
                        xs[column],
                        xs[column + 1],
                    )
                } else {
                    continue;
                };
                // 必须先完整计入这条边原本扫描的所有线段，再使用任何缓存或判断局部断边。
                if !budget.charge(lines.len()) {
                    return Ok(outcome(None, budget, true, checks));
                }
                let intervals = intervals
                    .get_or_insert_with(|| aligned_intervals(lines, &index, !right, coordinate));
                checks += intervals.len();
                match boundary_state(intervals, start, end) {
                    -1 => return Ok(outcome(None, budget, false, checks)),
                    0 => {
                        let neighbor_root = root(&mut parents, neighbor);
                        parents[neighbor_root] = root(&mut parents, current);
                    }
                    _ => present_edges.push((current, neighbor)),
                }
            }
        }
    }
    if present_edges
        .into_iter()
        .any(|(first, second)| root(&mut parents, first) == root(&mut parents, second))
    {
        return Ok(outcome(None, budget, false, checks));
    }
    let mut groups: Vec<Option<([usize; 4], usize)>> = vec![None; rows * columns];
    for current in 0..rows * columns {
        let group = root(&mut parents, current);
        let (row, column) = (current / columns, current % columns);
        let (bounds, count) = groups[group].get_or_insert(([row, column, row + 1, column + 1], 0));
        bounds[0] = bounds[0].min(row);
        bounds[1] = bounds[1].min(column);
        bounds[2] = bounds[2].max(row + 1);
        bounds[3] = bounds[3].max(column + 1);
        *count += 1;
    }
    let mut cells = Vec::new();
    for (bounds, count) in groups.into_iter().flatten() {
        if (bounds[2] - bounds[0]) * (bounds[3] - bounds[1]) != count {
            return Ok(outcome(None, budget, false, checks));
        }
        cells.push(bounds);
    }
    cells.sort_by_key(|cell| (cell[0], cell[1]));
    Ok(outcome(Some(cells), budget, false, checks))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 构造轻量线段，测试不依赖 Python 类型或浮点平均实现。
    fn line(horizontal: bool, coordinate: f64, start: f64, end: f64) -> AxisLine {
        AxisLine {
            horizontal,
            coordinate,
            start,
            end,
        }
    }

    /// 构造两行两列网格，保留原始水平优先顺序。
    fn grid() -> Vec<AxisLine> {
        vec![
            line(true, 0.0, 0.0, 10.0),
            line(true, 5.0, 0.0, 10.0),
            line(true, 10.0, 0.0, 10.0),
            line(false, 0.0, 0.0, 10.0),
            line(false, 5.0, 0.0, 10.0),
            line(false, 10.0, 0.0, 10.0),
        ]
    }

    /// 索引跳过大量无关线段，但预算仍计入全部原始配对。
    #[test]
    fn indexed_components_preserve_virtual_charges() {
        let mut lines = grid();
        lines.push(line(true, 100.0, 20.0, 25.0));
        let result = components(&lines, Budget::new(4, 100).unwrap()).unwrap();
        assert_eq!(result.value, Some(vec![vec![0, 1, 2, 3, 4, 5], vec![6]]));
        assert_eq!(result.budget_count, 25);
        assert!(result.actual_checks < 21);
        let result = components(&lines, Budget::new(4, 24).unwrap()).unwrap();
        assert!(result.exceeded);
        assert_eq!(result.budget_count, 25);
    }

    /// 标准网格输出按行列稳定排列，全部内部边仍逐条计入虚拟比较数。
    #[test]
    fn full_grid_cells_and_budget() {
        let result = logical_cells(
            &[0.0, 5.0, 10.0],
            &[0.0, 5.0, 10.0],
            &grid(),
            Budget::new(15, 100).unwrap(),
        )
        .unwrap();
        assert_eq!(
            result.value,
            Some(vec![[0, 0, 1, 1], [0, 1, 1, 2], [1, 0, 2, 1], [1, 1, 2, 2]])
        );
        assert_eq!(result.budget_count, 39);
        assert_eq!(result.actual_checks, 4);
    }

    /// 缺失边恢复跨列格，局部断边则在原第一条边处拒绝。
    #[test]
    fn merged_and_partial_edges() {
        let mut lines = grid();
        lines[4].start = 5.0;
        let merged = logical_cells(
            &[0.0, 5.0, 10.0],
            &[0.0, 5.0, 10.0],
            &lines,
            Budget::new(0, 100).unwrap(),
        )
        .unwrap();
        assert_eq!(
            merged.value,
            Some(vec![[0, 0, 1, 2], [1, 0, 2, 1], [1, 1, 2, 2]])
        );
        lines[4].start = 2.0;
        let partial = logical_cells(
            &[0.0, 5.0, 10.0],
            &[0.0, 5.0, 10.0],
            &lines,
            Budget::new(0, 100).unwrap(),
        )
        .unwrap();
        assert!(partial.value.is_none() && !partial.exceeded);
        assert_eq!(partial.budget_count, 6);
        let failure = logical_cells(
            &[0.0, 5.0, 10.0],
            &[0.0, 5.0, 10.0],
            &lines,
            Budget::new(0, 4).unwrap(),
        )
        .unwrap();
        assert!(failure.exceeded);
        assert_eq!(failure.budget_count, 5);
    }

    /// L形缺边及已有边绕路连通都不能变成矩形合并格。
    #[test]
    fn nonrectangular_and_conflicting_cells_are_rejected() {
        let mut lines = grid();
        lines[4].start = 5.0;
        lines[1].start = 5.0;
        assert!(logical_cells(
            &[0.0, 5.0, 10.0],
            &[0.0, 5.0, 10.0],
            &lines,
            Budget::new(0, 100).unwrap()
        )
        .unwrap()
        .value
        .is_none());
        lines[1].start = 0.0;
        lines[4].start = 10.0;
        lines[1].end = 5.0;
        assert!(logical_cells(
            &[0.0, 5.0, 10.0],
            &[0.0, 5.0, 10.0],
            &lines,
            Budget::new(0, 100).unwrap()
        )
        .unwrap()
        .value
        .is_none());
    }

    /// 坐标索引外扩一ULP后仍由原减法谓词裁决容差邻界。
    #[test]
    fn coordinate_tolerance_edges_are_not_filtered_out() {
        for coordinate in [0.6_f64.next_down(), 0.6, 0.6_f64.next_up()] {
            let lines = [line(true, 0.0, 0.0, 2.0), line(true, coordinate, 0.0, 2.0)];
            let result = components(&lines, Budget::new(0, 1).unwrap()).unwrap();
            assert_eq!(
                result.value.unwrap().len(),
                if coordinate <= 0.6 { 1 } else { 2 }
            );
        }
    }

    /// 差值在容差处舍入而区间端点抵消为零时，仍保留真实相邻候选。
    #[test]
    fn cancellation_around_zero_keeps_candidates() {
        for (coordinate, tiny) in [(-0.6, 1e-17), (0.6, -1e-17), (-0.6, 1e-300)] {
            let lines = [line(true, coordinate, 0.0, 2.0), line(true, tiny, 0.0, 2.0)];
            assert_eq!(
                components(&lines, Budget::new(0, 10).unwrap())
                    .unwrap()
                    .value,
                Some(vec![vec![0, 1]])
            );
            let vertical = [line(false, tiny, 0.0, 2.0)];
            assert_eq!(
                logical_cells(
                    &[-2.0, coordinate, 2.0],
                    &[0.0, 2.0],
                    &vertical,
                    Budget::new(0, 10).unwrap()
                )
                .unwrap()
                .value,
                Some(vec![[0, 0, 1, 1], [0, 1, 1, 2]])
            );
        }
    }

    /// 短区间即使没有对齐线也维持原末尾容差判定。
    #[test]
    fn empty_interval_short_edge_semantics() {
        assert_eq!(boundary_state(&[], 0.0, 0.8), 1);
        assert_eq!(boundary_state(&[], 0.0, 0.8_f64.next_up()), 0);
    }

    /// 已超限初始计数仍只加一，零比较不主动检查预算。
    #[test]
    fn existing_budget_failure_position_is_preserved() {
        let result = components(&grid(), Budget::new(9, 2).unwrap()).unwrap();
        assert_eq!(result.budget_count, 10);
        assert!(result.exceeded);
        let result = components(&[], Budget::new(9, 2).unwrap()).unwrap();
        assert_eq!(result.budget_count, 9);
        assert!(!result.exceeded);
    }
}
