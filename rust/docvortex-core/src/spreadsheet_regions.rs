//! 电子表格的连续区域发现与评分内核；输入为不可变坐标快照，不访问 Python。

use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::ops::Bound::{Excluded, Unbounded};

pub type Position = (i64, i64);
pub type SpanRecord = (i64, i64, i64, i64);
type Interval = (i128, i128);
type Rectangle = [i128; 4];

const EXACT_INTEGER_LIMIT: u128 = 1_u128 << 53;
const PREFERENCE_MARGIN: f64 = 0.15;

#[derive(Clone, Debug, Default)]
pub struct RegionInput {
    pub starts: Vec<Position>,
    pub content_points: Vec<Position>,
    /// 连通性矩形使用闭区间，字段顺序为行起、行止、列起、列止。
    pub content_rectangles: Vec<[i64; 4]>,
    pub semantic_positions: Vec<Position>,
    /// 跨度独立于语义内容，未提供的坐标默认是 1×1；重复坐标保留最后记录。
    pub spans: Vec<SpanRecord>,
    pub max_row: i64,
    pub max_col: i64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConnectedRegion {
    /// 保持右、左、下、上的 BFS 首次加入顺序，调用方可转换成集合。
    pub cells: Vec<Position>,
    pub row_start: i64,
    pub row_end: i64,
    pub col_start: i64,
    pub col_end: i64,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct RegionSummary {
    pub real_singleton_ratio: f64,
    pub weighted_blank_ratio: f64,
    pub interior_blank_line_ratio: f64,
    pub sparse_large_table_ratio: f64,
    pub severe_separator_count: usize,
    pub row_overlap_excess_ratio: f64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct GapCandidate {
    pub gap_tolerance: i64,
    pub penalty: f64,
    pub regions: Vec<ConnectedRegion>,
    pub summary: RegionSummary,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Selection {
    pub gap_tolerance: i64,
    pub penalty: f64,
    pub regions: Vec<ConnectedRegion>,
    pub kept_indices: Vec<usize>,
    pub candidates: Vec<GapCandidate>,
}

struct PreparedInput<'a> {
    input: &'a RegionInput,
    content: HashSet<Position>,
    content_rows: BTreeMap<i64, Vec<(i64, i64)>>,
    content_rectangles: RectangleIndex,
    semantic_rows: BTreeMap<i64, Vec<i64>>,
    spans: HashMap<Position, (i64, i64)>,
}

impl<'a> PreparedInput<'a> {
    /// 一次建立连通性、语义行和跨度索引，三个 gap 候选复用同一份只读证据。
    fn new(input: &'a RegionInput) -> Result<Self, &'static str> {
        if input
            .content_rectangles
            .iter()
            .any(|rect| rect[0] > rect[1] || rect[2] > rect[3])
        {
            return Err("spreadsheet content rectangles must use ordered closed bounds");
        }
        let mut semantic_rows: BTreeMap<i64, Vec<i64>> = BTreeMap::new();
        for &(row, col) in &input.semantic_positions {
            semantic_rows.entry(row).or_default().push(col);
        }
        for columns in semantic_rows.values_mut() {
            columns.sort_unstable();
            columns.dedup();
        }
        let mut content_rows: BTreeMap<i64, Vec<(i64, i64)>> = BTreeMap::new();
        let mut rectangles = Vec::new();
        for &rectangle in &input.content_rectangles {
            if rectangle[0] == rectangle[1] {
                content_rows
                    .entry(rectangle[0])
                    .or_default()
                    .push((rectangle[2], rectangle[3]));
            } else {
                rectangles.push(rectangle);
            }
        }
        for intervals in content_rows.values_mut() {
            intervals.sort_unstable();
            let mut merged: Vec<(i64, i64)> = Vec::with_capacity(intervals.len());
            for &(start, end) in intervals.iter() {
                if let Some(previous) = merged.last_mut().filter(|previous| start <= previous.1) {
                    previous.1 = previous.1.max(end);
                } else {
                    merged.push((start, end));
                }
            }
            *intervals = merged;
        }
        Ok(Self {
            input,
            content: input.content_points.iter().copied().collect(),
            content_rows,
            content_rectangles: RectangleIndex::new(rectangles),
            semantic_rows,
            spans: input
                .spans
                .iter()
                .map(|&(row, col, rows, cols)| ((row, col), (rows, cols)))
                .collect(),
        })
    }

    /// 只在原始工作表边界内检查内容；合并覆盖与普通内容点等价参与连通。
    fn has_content(&self, row: i64, col: i64) -> bool {
        row >= 0
            && col >= 0
            && row <= self.input.max_row
            && col <= self.input.max_col
            && (self.content.contains(&(row, col))
                || self.content_rows.get(&row).is_some_and(|intervals| {
                    let end = intervals.partition_point(|interval| interval.0 <= col);
                    end > 0 && col <= intervals[end - 1].1
                })
                || self.content_rectangles.contains(row, col))
    }

    /// 返回原点跨度，负数或零跨度仍保留给后续 Python range 等价判定。
    fn span_at(&self, position: Position) -> (i64, i64) {
        self.spans.get(&position).copied().unwrap_or((1, 1))
    }

    /// 仅查询 bbox 内实际语义坐标，不把连通 cells 或合并覆盖格当作语义起点。
    fn semantic_positions(&self, region: &ConnectedRegion) -> Vec<Position> {
        let mut positions = Vec::new();
        for (&row, columns) in self.semantic_rows.range(region.row_start..=region.row_end) {
            let first = columns.partition_point(|col| *col < region.col_start);
            let end = columns.partition_point(|col| *col <= region.col_end);
            positions.extend(columns[first..end].iter().map(|&col| (row, col)));
        }
        positions
    }

    /// 按传入起点顺序发现区域，全局访问表只跳过后续起点，不限制一次 BFS 的成员。
    fn discover(&self, gap_tolerance: i64) -> Vec<ConnectedRegion> {
        let mut visited = HashSet::new();
        let mut regions = Vec::new();
        for &start in &self.input.starts {
            if visited.contains(&start) {
                continue;
            }
            let region = self.flood_fill(start, gap_tolerance);
            visited.extend(region.cells.iter().copied());
            regions.push(region);
        }
        regions
    }

    /// 无条件加入起点，按右左下上逐步寻找最近内容格或当前区域阻挡点。
    fn flood_fill(&self, start: Position, gap_tolerance: i64) -> ConnectedRegion {
        let mut queue = VecDeque::from([start]);
        let mut members = HashSet::from([start]);
        let mut cells = vec![start];
        let (mut row_start, mut row_end, mut col_start, mut col_end) =
            (start.0, start.0, start.1, start.1);
        let last_step = i128::from(gap_tolerance) + 1;
        while let Some((row, col)) = queue.pop_front() {
            row_start = row_start.min(row);
            row_end = row_end.max(row);
            col_start = col_start.min(col);
            col_end = col_end.max(col);
            for (dr, dc) in [(0_i128, 1_i128), (0, -1), (1, 0), (-1, 0)] {
                // 负 gap 对应 Python 的空 range，保留只含起点的区域。
                for step in 1..=last_step {
                    let next_row = i128::from(row) + dr * step;
                    let next_col = i128::from(col) + dc * step;
                    if let (Ok(nr), Ok(nc)) = (i64::try_from(next_row), i64::try_from(next_col)) {
                        let position = (nr, nc);
                        if members.contains(&position) {
                            break;
                        }
                        if self.has_content(nr, nc) {
                            members.insert(position);
                            cells.push(position);
                            queue.push_back(position);
                            break;
                        }
                    }
                    // 离开边界且继续向外时不存在更远的内容；不改变最近成员的停止语义。
                    if (dr <= 0 && next_row < 0)
                        || (dr >= 0 && next_row > i128::from(self.input.max_row))
                        || (dc <= 0 && next_col < 0)
                        || (dc >= 0 && next_col > i128::from(self.input.max_col))
                    {
                        break;
                    }
                }
            }
        }
        ConnectedRegion {
            cells,
            row_start,
            row_end,
            col_start,
            col_end,
        }
    }

    /// 用稀疏跨度矩形统计一组候选，浮点累加顺序与 Python 区域顺序完全一致。
    fn summarize(&self, regions: &[ConnectedRegion]) -> Result<RegionSummary, &'static str> {
        let mut real_singletons = 0_u128;
        let mut sparse_large_tables = 0_u128;
        let mut severe_separator_count = 0_usize;
        let mut total_area = 0_u128;
        let mut weighted_blank_numerator = 0.0_f64;
        let mut total_interior_blank_lines = 0_u128;
        let mut total_possible_interior_lines = 0_u128;
        let mut total_row_coverage = 0_u128;
        let mut row_intervals = Vec::with_capacity(regions.len());
        for region in regions {
            let (rows, cols) = dimensions(region)?;
            let area = rows
                .checked_mul(cols)
                .ok_or("spreadsheet region area overflows")?;
            exact_integer(area)?;
            let rectangles = self.semantic_rectangles(region);
            let content_area = rectangle_union_area(&rectangles)?;
            let blank_ratio = 1.0 - exact_ratio(content_area, area.max(1))?;
            let row_cover: Vec<_> = rectangles.iter().map(|rect| (rect[0], rect[1])).collect();
            let col_cover: Vec<_> = rectangles.iter().map(|rect| (rect[2], rect[3])).collect();
            let (blank_rows, longest_rows) = blank_interval_statistics(
                row_cover,
                i128::from(region.row_start) + 1,
                i128::from(region.row_end),
            );
            let (blank_cols, longest_cols) = blank_interval_statistics(
                col_cover,
                i128::from(region.col_start) + 1,
                i128::from(region.col_end),
            );
            total_area = checked_sum(total_area, area)?;
            // 必须先计算每个区域的比例，再乘面积并逐项相加，不能代数改写为空格计数。
            weighted_blank_numerator += (area as f64) * blank_ratio;
            total_interior_blank_lines = checked_sum(
                total_interior_blank_lines,
                checked_sum(blank_rows, blank_cols)?,
            )?;
            total_possible_interior_lines = checked_sum(
                total_possible_interior_lines,
                checked_sum(rows.saturating_sub(2), cols.saturating_sub(2))?,
            )?;
            total_row_coverage = checked_sum(total_row_coverage, rows)?;
            row_intervals.push((i128::from(region.row_start), i128::from(region.row_end) + 1));
            if rows == 1
                && cols == 1
                && self.span_at((region.row_start, region.col_start)) == (1, 1)
            {
                real_singletons += 1;
            }
            if area >= 6 && blank_ratio > 0.35 {
                sparse_large_tables += 1;
            }
            if longest_rows.max(longest_cols) >= 2 {
                severe_separator_count += 1;
            }
        }
        let occupied_rows = union_interval_length(row_intervals);
        let overlap_excess = total_row_coverage
            .checked_sub(occupied_rows)
            .ok_or("spreadsheet row coverage is inconsistent")?;
        let table_count = (regions.len() as u128).max(1);
        Ok(RegionSummary {
            real_singleton_ratio: exact_ratio(real_singletons, table_count)?,
            weighted_blank_ratio: weighted_blank_numerator / exact_integer(total_area.max(1))?,
            interior_blank_line_ratio: exact_ratio(
                total_interior_blank_lines,
                total_possible_interior_lines.max(1),
            )?,
            sparse_large_table_ratio: exact_ratio(sparse_large_tables, table_count)?,
            severe_separator_count,
            row_overlap_excess_ratio: exact_ratio(overlap_excess, occupied_rows.max(1))?,
        })
    }

    /// 将 bbox 内的语义原点展开为裁剪后的半开矩形，不按巨大包围盒逐格分配。
    fn semantic_rectangles(&self, region: &ConnectedRegion) -> Vec<Rectangle> {
        self.semantic_positions(region)
            .into_iter()
            .filter_map(|position| {
                let (row_span, col_span) = self.span_at(position);
                if row_span <= 0 || col_span <= 0 {
                    return None;
                }
                let row = i128::from(position.0);
                let col = i128::from(position.1);
                Some([
                    row,
                    (row + i128::from(row_span)).min(i128::from(region.row_end) + 1),
                    col,
                    (col + i128::from(col_span)).min(i128::from(region.col_end) + 1),
                ])
            })
            .collect()
    }
}

struct RectangleNode {
    bounds: [i64; 4],
    children: Option<(usize, usize)>,
}

#[derive(Default)]
struct RectangleIndex {
    nodes: Vec<RectangleNode>,
}

impl RectangleIndex {
    /// 为跨多行的通用覆盖矩形构建二维包围盒树，单行区间由独立行索引处理。
    fn new(mut rectangles: Vec<[i64; 4]>) -> Self {
        let mut index = Self::default();
        if !rectangles.is_empty() {
            index.build(&mut rectangles);
        }
        index
    }

    /// 沿包围盒较长的轴分割矩形，只保存与输入数量成比例的树节点。
    fn build(&mut self, rectangles: &mut [[i64; 4]]) -> usize {
        let mut bounds = rectangles[0];
        for rectangle in &rectangles[1..] {
            bounds[0] = bounds[0].min(rectangle[0]);
            bounds[1] = bounds[1].max(rectangle[1]);
            bounds[2] = bounds[2].min(rectangle[2]);
            bounds[3] = bounds[3].max(rectangle[3]);
        }
        let node = self.nodes.len();
        self.nodes.push(RectangleNode {
            bounds,
            children: None,
        });
        if rectangles.len() > 1 {
            let axis = if i128::from(bounds[1]) - i128::from(bounds[0])
                >= i128::from(bounds[3]) - i128::from(bounds[2])
            {
                0
            } else {
                2
            };
            rectangles.sort_unstable_by_key(|rectangle| {
                i128::from(rectangle[axis]) + i128::from(rectangle[axis + 1])
            });
            let middle = rectangles.len() / 2;
            let (first, second) = rectangles.split_at_mut(middle);
            let left = self.build(first);
            let right = self.build(second);
            self.nodes[node].children = Some((left, right));
        }
        node
    }

    /// 只访问包含查询点的树分支，避免每个 BFS 邻居线性扫描所有合并矩形。
    fn contains(&self, row: i64, col: i64) -> bool {
        !self.nodes.is_empty() && self.contains_node(0, row, col)
    }

    /// 复用平衡树的递归栈检查一个查询点，避免每次邻居查询分配临时向量。
    fn contains_node(&self, index: usize, row: i64, col: i64) -> bool {
        let node = &self.nodes[index];
        let bounds = node.bounds;
        if row < bounds[0] || row > bounds[1] || col < bounds[2] || col > bounds[3] {
            return false;
        }
        match node.children {
            Some((left, right)) => {
                self.contains_node(left, row, col) || self.contains_node(right, row, col)
            }
            None => true,
        }
    }
}

/// 检查闭区间包围盒，并以更宽的整数避免高坐标差值溢出。
fn dimensions(region: &ConnectedRegion) -> Result<(u128, u128), &'static str> {
    if region.row_start > region.row_end || region.col_start > region.col_end {
        return Err("spreadsheet region bounds must be ordered");
    }
    Ok((
        (i128::from(region.row_end) - i128::from(region.row_start) + 1) as u128,
        (i128::from(region.col_end) - i128::from(region.col_start) + 1) as u128,
    ))
}

/// 只接收能精确转为 f64 的评分整数，超出范围显式报错，不静默改变 Python 除法。
fn exact_integer(value: u128) -> Result<f64, &'static str> {
    if value > EXACT_INTEGER_LIMIT {
        return Err("spreadsheet summary integer exceeds exact f64 range (2^53)");
    }
    Ok(value as f64)
}

/// 在整数均可精确表示时复现 Python int/int 的正确舍入结果。
fn exact_ratio(numerator: u128, denominator: u128) -> Result<f64, &'static str> {
    Ok(exact_integer(numerator)? / exact_integer(denominator)?)
}

/// 为累计形态统计提供有检查的整数加法，并限制后续浮点转换的精确范围。
fn checked_sum(first: u128, second: u128) -> Result<u128, &'static str> {
    let sum = first
        .checked_add(second)
        .ok_or("spreadsheet summary integer overflows")?;
    exact_integer(sum)?;
    Ok(sum)
}

/// 合并半开覆盖区间，准确统计巨大稀疏坐标域内的覆盖长度。
fn union_interval_length(mut intervals: Vec<Interval>) -> u128 {
    intervals.sort_unstable();
    let Some(&(mut start, mut end)) = intervals.first() else {
        return 0;
    };
    let mut length = 0_u128;
    for &(next_start, next_end) in &intervals[1..] {
        if next_start <= end {
            end = end.max(next_end);
        } else {
            length += (end - start) as u128;
            (start, end) = (next_start, next_end);
        }
    }
    length + (end - start) as u128
}

/// 在内部行或列的半开范围内统计空线总量及最长连续空档，不枚举空白位置。
fn blank_interval_statistics(
    mut intervals: Vec<Interval>,
    lower: i128,
    upper: i128,
) -> (u128, u128) {
    if lower >= upper {
        return (0, 0);
    }
    intervals.sort_unstable();
    let mut cursor = lower;
    let mut count = 0_u128;
    let mut longest = 0_u128;
    for (start, end) in intervals {
        let start = start.max(lower);
        let end = end.min(upper);
        if start >= end {
            continue;
        }
        if start > cursor {
            let gap = (start - cursor) as u128;
            count += gap;
            longest = longest.max(gap);
        }
        cursor = cursor.max(end);
    }
    if cursor < upper {
        let gap = (upper - cursor) as u128;
        count += gap;
        longest = longest.max(gap);
    }
    (count, longest)
}

struct CoverageTree {
    coordinates: Vec<i128>,
    counts: Vec<i64>,
    lengths: Vec<u128>,
}

impl CoverageTree {
    /// 为实际矩形端点构建列覆盖树，存储量与语义点数量相关而非 bbox 宽度。
    fn new(mut coordinates: Vec<i128>) -> Result<Self, &'static str> {
        coordinates.sort_unstable();
        coordinates.dedup();
        let size = coordinates
            .len()
            .checked_mul(4)
            .ok_or("spreadsheet coverage tree overflows")?;
        Ok(Self {
            coordinates,
            counts: vec![0; size],
            lengths: vec![0; size],
        })
    }

    /// 增减一个半开列区间，矩形的加入和移除使用相同的端点分割。
    fn change(&mut self, lower: i128, upper: i128, delta: i64) -> Result<(), &'static str> {
        let first = self
            .coordinates
            .binary_search(&lower)
            .map_err(|_| "missing coverage endpoint")?;
        let end = self
            .coordinates
            .binary_search(&upper)
            .map_err(|_| "missing coverage endpoint")?;
        self.update(1, 0, self.coordinates.len() - 1, first, end, delta)
    }

    /// 递归更新覆盖计数，并在未被整个覆盖时合并两个子区间长度。
    fn update(
        &mut self,
        node: usize,
        left: usize,
        right: usize,
        first: usize,
        end: usize,
        delta: i64,
    ) -> Result<(), &'static str> {
        if first >= right || end <= left {
            return Ok(());
        }
        if first <= left && right <= end {
            self.counts[node] = self.counts[node]
                .checked_add(delta)
                .ok_or("spreadsheet coverage count overflows")?;
            if self.counts[node] < 0 {
                return Err("spreadsheet coverage count became negative");
            }
        } else {
            let middle = (left + right) / 2;
            self.update(node * 2, left, middle, first, end, delta)?;
            self.update(node * 2 + 1, middle, right, first, end, delta)?;
        }
        self.lengths[node] = if self.counts[node] > 0 {
            (self.coordinates[right] - self.coordinates[left]) as u128
        } else if right - left == 1 {
            0
        } else {
            self.lengths[node * 2] + self.lengths[node * 2 + 1]
        };
        Ok(())
    }

    /// 返回当前扫描行带上的列并集长度，不暴露树的内部划分。
    fn covered_length(&self) -> u128 {
        self.lengths.get(1).copied().unwrap_or(0)
    }
}

/// 用行事件扫描与列覆盖树计算重叠跨度的整数并集面积。
fn rectangle_union_area(rectangles: &[Rectangle]) -> Result<u128, &'static str> {
    if rectangles.is_empty() {
        return Ok(0);
    }
    let mut events = Vec::with_capacity(rectangles.len() * 2);
    let mut columns = Vec::with_capacity(rectangles.len() * 2);
    for rect in rectangles {
        events.push((rect[0], rect[2], rect[3], 1_i64));
        events.push((rect[1], rect[2], rect[3], -1_i64));
        columns.extend([rect[2], rect[3]]);
    }
    events.sort_unstable_by_key(|event| event.0);
    let mut tree = CoverageTree::new(columns)?;
    let mut previous_row = events[0].0;
    let mut area = 0_u128;
    let mut index = 0;
    while index < events.len() {
        let row = events[index].0;
        let strip = tree
            .covered_length()
            .checked_mul((row - previous_row) as u128)
            .ok_or("spreadsheet covered area overflows")?;
        area = area
            .checked_add(strip)
            .ok_or("spreadsheet covered area overflows")?;
        while index < events.len() && events[index].0 == row {
            let (_, lower, upper, delta) = events[index];
            tree.change(lower, upper, delta)?;
            index += 1;
        }
        previous_row = row;
    }
    Ok(area)
}

/// 按指定 gap 执行一次区域发现，包含负 gap 的原始空邻域语义。
pub fn discover_connected_regions(
    input: &RegionInput,
    gap_tolerance: i64,
) -> Result<Vec<ConnectedRegion>, &'static str> {
    Ok(PreparedInput::new(input)?.discover(gap_tolerance))
}

/// 为给定候选集合提供与 Python 同字段、同浮点顺序的完整评分统计。
pub fn summarize_connected_regions(
    input: &RegionInput,
    regions: &[ConnectedRegion],
) -> Result<RegionSummary, &'static str> {
    PreparedInput::new(input)?.summarize(regions)
}

/// 按原表达式的从左到右顺序施加固定权重，不合并代数项或采用融合乘加。
pub fn gap_candidate_penalty(summary: &RegionSummary) -> f64 {
    let value = 6.0 * summary.severe_separator_count as f64;
    let value = value + 2.5 * summary.interior_blank_line_ratio;
    let value = value + 1.5 * summary.sparse_large_table_ratio;
    let value = value + 1.0 * summary.real_singleton_ratio;
    let value = value + 0.5 * summary.weighted_blank_ratio;
    value + 1.0 * summary.row_overlap_excess_ratio
}

/// 先按精确 0.15 邻域筛选，再按严重分隔、1/0/2 偏好与剩余指标稳定选择。
pub fn choose_candidates(candidates: &[GapCandidate]) -> Result<usize, &'static str> {
    if candidates.is_empty() {
        return Err("spreadsheet candidate collection must not be empty");
    }
    if candidates.iter().any(|candidate| {
        !matches!(candidate.gap_tolerance, 0..=2)
            || !candidate.penalty.is_finite()
            || !candidate.summary.interior_blank_line_ratio.is_finite()
    }) {
        return Err("spreadsheet candidates require finite scores and gap 0, 1 or 2");
    }
    let minimum = candidates
        .iter()
        .map(|candidate| candidate.penalty)
        .reduce(f64::min)
        .unwrap();
    let near_limit = minimum + PREFERENCE_MARGIN;
    let mut selected = None;
    for (index, candidate) in candidates.iter().enumerate() {
        if candidate.penalty > near_limit {
            continue;
        }
        let preference = [1_usize, 0, 2][candidate.gap_tolerance as usize];
        let key = (
            candidate.summary.severe_separator_count,
            preference,
            candidate.summary.interior_blank_line_ratio,
            candidate.penalty,
        );
        if selected.is_none_or(|(_, previous)| key < previous) {
            selected = Some((index, key));
        }
    }
    selected
        .map(|(index, _)| index)
        .ok_or("spreadsheet candidate selection has no finite minimum")
}

/// 对排序后的坐标序列执行严格子集检查，相等集合明确不成立。
fn is_strict_subset(first: &[Position], second: &[Position]) -> bool {
    if first.len() >= second.len() {
        return false;
    }
    let mut index = 0;
    for position in first {
        while index < second.len() && second[index] < *position {
            index += 1;
        }
        if second.get(index) != Some(position) {
            return false;
        }
        index += 1;
    }
    true
}

/// 按集合长度只检查严格更大的候选，等长集合无需两两扫描，输出仍保持原索引顺序。
pub fn keep_maximal_by_semantic_sets(semantic_sets: &[Vec<Position>]) -> Vec<usize> {
    let normalized: Vec<_> = semantic_sets
        .iter()
        .map(|positions| {
            let mut positions = positions.clone();
            positions.sort_unstable();
            positions.dedup();
            positions
        })
        .collect();
    let mut by_size: BTreeMap<usize, Vec<&[Position]>> = BTreeMap::new();
    for positions in &normalized {
        by_size.entry(positions.len()).or_default().push(positions);
    }
    normalized
        .iter()
        .enumerate()
        .filter_map(|(index, positions)| {
            let contained =
                by_size
                    .range((Excluded(positions.len()), Unbounded))
                    .any(|(_, candidates)| {
                        candidates
                            .iter()
                            .any(|other| is_strict_subset(positions, other))
                    });
            (!contained).then_some(index)
        })
        .collect()
}

/// 在一次纯数值调用中完成发现、评分、自动或固定 gap 选择以及 bbox 语义子集筛选。
pub fn analyze_regions(
    input: RegionInput,
    gap_tolerance: Option<i64>,
) -> Result<Selection, &'static str> {
    let prepared = PreparedInput::new(&input)?;
    let gaps = gap_tolerance.map_or_else(|| vec![0, 1, 2], |gap| vec![gap]);
    let mut candidates = Vec::with_capacity(gaps.len());
    for gap in gaps {
        let regions = prepared.discover(gap);
        let summary = prepared.summarize(&regions)?;
        let penalty = gap_candidate_penalty(&summary);
        candidates.push(GapCandidate {
            gap_tolerance: gap,
            penalty,
            regions,
            summary,
        });
    }
    let selected = if gap_tolerance.is_some() {
        0
    } else {
        choose_candidates(&candidates)?
    };
    let best = &candidates[selected];
    let semantic_sets: Vec<_> = best
        .regions
        .iter()
        .map(|region| prepared.semantic_positions(region))
        .collect();
    Ok(Selection {
        gap_tolerance: best.gap_tolerance,
        penalty: best.penalty,
        regions: best.regions.clone(),
        kept_indices: keep_maximal_by_semantic_sets(&semantic_sets),
        candidates,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 以最小样本构造评分候选，单独裁决阈值和偏好而不依赖表格数据生成。
    fn candidate(gap: i64, penalty: f64, severe: usize) -> GapCandidate {
        GapCandidate {
            gap_tolerance: gap,
            penalty,
            regions: Vec::new(),
            summary: RegionSummary {
                severe_separator_count: severe,
                ..RegionSummary::default()
            },
        }
    }

    /// 无内容起点仍加入，最近点停止和右左下上 BFS 顺序不能因集合顺序改变。
    #[test]
    fn starts_and_nearest_neighbor_order_are_preserved() {
        let input = RegionInput {
            starts: vec![(0, 1)],
            content_points: vec![(0, 0), (0, 2), (0, 4)],
            max_row: 0,
            max_col: 4,
            ..RegionInput::default()
        };
        let regions = discover_connected_regions(&input, 2).unwrap();
        assert_eq!(regions[0].cells, vec![(0, 1), (0, 2), (0, 0), (0, 4)]);
        let cross = RegionInput {
            starts: vec![(1, 1)],
            content_points: vec![(1, 2), (1, 0), (2, 1), (0, 1)],
            max_row: 2,
            max_col: 2,
            ..RegionInput::default()
        };
        assert_eq!(
            discover_connected_regions(&cross, 0).unwrap()[0].cells,
            vec![(1, 1), (1, 2), (1, 0), (2, 1), (0, 1)]
        );
    }

    /// 全局已访问集只跳过起点，两个不同空起点仍可分别到达同一内容集合。
    #[test]
    fn global_visited_only_skips_later_seeds() {
        let input = RegionInput {
            starts: vec![(0, 1), (0, 3), (0, 2), (0, 1)],
            content_points: vec![(0, 0), (0, 2)],
            max_row: 0,
            max_col: 3,
            ..RegionInput::default()
        };
        let regions = discover_connected_regions(&input, 1).unwrap();
        assert_eq!(regions.len(), 2);
        assert_eq!(regions[1].cells, vec![(0, 3), (0, 2), (0, 0)]);
        let negative = discover_connected_regions(&input, -1).unwrap();
        assert_eq!(
            negative
                .iter()
                .map(|region| region.cells.clone())
                .collect::<Vec<_>>(),
            vec![vec![(0, 1)], vec![(0, 3)], vec![(0, 2)]]
        );
    }

    /// 连通矩形使用闭区间，超出工作表边界的覆盖不能被误加入区域。
    #[test]
    fn merged_connectivity_is_clipped_to_sheet_bounds() {
        let input = RegionInput {
            starts: vec![(0, 0)],
            content_rectangles: vec![[0, 2, 0, 2]],
            max_row: 1,
            max_col: 1,
            ..RegionInput::default()
        };
        let regions = discover_connected_regions(&input, 0).unwrap();
        assert_eq!(regions[0].cells, vec![(0, 0), (0, 1), (1, 0), (1, 1)]);
    }

    /// 单行区间和多行矩形索引都必须等价于原始闭区间并集，包含相接和重叠边界。
    #[test]
    fn indexed_connectivity_matches_direct_rectangle_membership() {
        let input = RegionInput {
            content_points: vec![(11, 11)],
            content_rectangles: vec![
                [0, 0, 0, 3],
                [0, 0, 2, 5],
                [0, 0, 8, 9],
                [2, 6, 0, 2],
                [3, 8, 4, 7],
                [7, 10, 2, 6],
            ],
            max_row: 11,
            max_col: 11,
            ..RegionInput::default()
        };
        let prepared = PreparedInput::new(&input).unwrap();
        for row in -1..=12 {
            for col in -1..=12 {
                let expected = (0..=11).contains(&row)
                    && (0..=11).contains(&col)
                    && (input.content_points.contains(&(row, col))
                        || input.content_rectangles.iter().any(|rect| {
                            rect[0] <= row && row <= rect[1] && rect[2] <= col && col <= rect[3]
                        }));
                assert_eq!(prepared.has_content(row, col), expected, "({row}, {col})");
            }
        }
    }

    /// 语义矩形按 bbox 内原点取样，跨界裁剪并对重叠跨度只计一次覆盖。
    #[test]
    fn summaries_cover_bbox_semantics_and_union_spans() {
        let region = ConnectedRegion {
            cells: vec![(0, 0), (2, 2)],
            row_start: 0,
            row_end: 2,
            col_start: 0,
            col_end: 2,
        };
        let input = RegionInput {
            semantic_positions: vec![(0, 0), (1, 1), (8, 8)],
            spans: vec![(0, 0, 3, 2), (1, 1, 8, 8)],
            ..RegionInput::default()
        };
        let summary = summarize_connected_regions(&input, &[region]).unwrap();
        assert_eq!(summary.weighted_blank_ratio, 1.0 - 8.0 / 9.0);
        assert_eq!(summary.interior_blank_line_ratio, 0.0);
        assert_eq!(summary.severe_separator_count, 0);
        assert_eq!(summary.real_singleton_ratio, 0.0);
    }

    /// 空语义单格也需查跨度；零或负跨度不应被强制当作 1×1 可见内容。
    #[test]
    fn nonsemantic_singleton_and_nonpositive_spans_are_preserved() {
        let region = ConnectedRegion {
            cells: vec![(5, 9)],
            row_start: 5,
            row_end: 5,
            col_start: 9,
            col_end: 9,
        };
        let input = RegionInput {
            spans: vec![(5, 9, 3, 4)],
            ..RegionInput::default()
        };
        let summary = summarize_connected_regions(&input, std::slice::from_ref(&region)).unwrap();
        assert_eq!(summary.real_singleton_ratio, 0.0);
        assert_eq!(summary.weighted_blank_ratio, 1.0);
        let empty_span = RegionInput {
            semantic_positions: vec![(5, 9)],
            spans: vec![(5, 9, 0, 1)],
            ..RegionInput::default()
        };
        assert_eq!(
            summarize_connected_regions(&empty_span, &[region])
                .unwrap()
                .weighted_blank_ratio,
            1.0
        );
    }

    /// 巨大稀疏包围盒的空线和面积由区间统计得出，不分配逐格或逐行布尔矩阵。
    #[test]
    fn sparse_large_bounds_use_interval_statistics() {
        let region = ConnectedRegion {
            cells: vec![(0, 0), (999_999, 999_999)],
            row_start: 0,
            row_end: 999_999,
            col_start: 0,
            col_end: 999_999,
        };
        let input = RegionInput {
            semantic_positions: region.cells.clone(),
            ..RegionInput::default()
        };
        let summary = summarize_connected_regions(&input, &[region]).unwrap();
        assert_eq!(
            summary.weighted_blank_ratio,
            1.0 - 2.0 / 1_000_000_000_000.0
        );
        assert_eq!(summary.interior_blank_line_ratio, 1.0);
        assert_eq!(summary.severe_separator_count, 1);
        assert_eq!(summary.sparse_large_table_ratio, 1.0);
    }

    /// 行重叠统计包含整个 bbox 行带，不能仅统计存在语义内容的行。
    #[test]
    fn row_overlap_counts_full_bounding_intervals() {
        let regions = vec![
            ConnectedRegion {
                cells: Vec::new(),
                row_start: 0,
                row_end: 2,
                col_start: 0,
                col_end: 0,
            },
            ConnectedRegion {
                cells: Vec::new(),
                row_start: 1,
                row_end: 3,
                col_start: 5,
                col_end: 5,
            },
        ];
        let summary = summarize_connected_regions(&RegionInput::default(), &regions).unwrap();
        assert_eq!(summary.row_overlap_excess_ratio, 0.5);
    }

    /// 0.15 边界精确包含，超过一个 ULP 即排除；严重分隔优先于 1/0/2 偏好。
    #[test]
    fn candidate_margin_uses_exact_ulps_and_stable_preferences() {
        let boundary = 0.15_f64;
        assert_eq!(
            choose_candidates(&[
                candidate(0, 0.0, 0),
                candidate(1, boundary, 0),
                candidate(2, 0.0, 0)
            ]),
            Ok(1)
        );
        let outside = f64::from_bits(boundary.to_bits() + 1);
        assert_eq!(
            choose_candidates(&[
                candidate(0, 0.0, 0),
                candidate(1, outside, 0),
                candidate(2, 0.0, 0)
            ]),
            Ok(0)
        );
        let inside = f64::from_bits(boundary.to_bits() - 1);
        assert_eq!(
            choose_candidates(&[
                candidate(0, 0.0, 0),
                candidate(1, inside, 1),
                candidate(2, 0.0, 0)
            ]),
            Ok(0)
        );
        assert_eq!(
            choose_candidates(&[candidate(2, 0.0, 0), candidate(0, 0.0, 0)]),
            Ok(1)
        );
    }

    /// 严格包含会删除空集和真子集，重复最大集合与空空集合都按原序保留。
    #[test]
    fn equal_semantic_sets_are_never_deduplicated() {
        assert_eq!(keep_maximal_by_semantic_sets(&[vec![], vec![]]), vec![0, 1]);
        assert_eq!(
            keep_maximal_by_semantic_sets(&[
                vec![],
                vec![(1, 1)],
                vec![(1, 1), (2, 2)],
                vec![(2, 2), (1, 1), (1, 1)],
            ]),
            vec![2, 3]
        );
    }

    /// 大量离散单格无需任何更大集合查询结果，重复单格和原始乱序也应完整保留。
    #[test]
    fn equal_size_singletons_keep_original_order() {
        let mut sets: Vec<_> = (0..1024).rev().map(|index| vec![(index, index)]).collect();
        sets.extend([vec![(7, 7)], vec![(1, 1)]]);
        assert_eq!(
            keep_maximal_by_semantic_sets(&sets),
            (0..sets.len()).collect::<Vec<_>>()
        );
    }

    /// 空起点保持三个空候选并优先 gap=1，高坐标不影响小范围整数统计。
    #[test]
    fn automatic_selection_preserves_empty_input_and_high_coordinates() {
        let empty = analyze_regions(RegionInput::default(), None).unwrap();
        assert_eq!(empty.gap_tolerance, 1);
        assert!(empty.regions.is_empty());
        assert_eq!(empty.candidates.len(), 3);
        let position = (i64::MAX - 2, i64::MAX - 2);
        let input = RegionInput {
            starts: vec![position],
            content_points: vec![position],
            semantic_positions: vec![position],
            max_row: i64::MAX,
            max_col: i64::MAX,
            ..RegionInput::default()
        };
        assert_eq!(analyze_regions(input, None).unwrap().penalty, 1.0);
    }

    /// 超出精确整数范围必须显式拒绝，而非因有界实现改变 Python 的评分舍入。
    #[test]
    fn summary_rejects_inexact_integer_ranges() {
        let region = ConnectedRegion {
            cells: Vec::new(),
            row_start: 0,
            row_end: 0,
            col_start: 0,
            col_end: (1_i64 << 53),
        };
        assert_eq!(
            summarize_connected_regions(&RegionInput::default(), &[region]),
            Err("spreadsheet summary integer exceeds exact f64 range (2^53)")
        );
    }
}
