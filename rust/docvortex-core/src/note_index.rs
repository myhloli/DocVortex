//! The persistent height count tree of the vertical prefix only saves the value of this candidate construction.

#[derive(Clone, Copy, Default)]
struct Node {
    left: usize,
    right: usize,
    count: usize,
}

pub struct HeightIndex {
    nodes: Vec<Node>,
    roots: Vec<usize>,
    centers: Vec<f64>,
    heights: Vec<f64>,
}

impl HeightIndex {
    /// The height draw retains the input order, the center sorting is also stable, and the query does not copy height samples.
    pub fn new(items: &[(i64, f64, f64)]) -> Self {
        let mut order: Vec<usize> = (0..items.len()).collect();
        order.sort_by(|&a, &b| items[a].2.partial_cmp(&items[b].2).unwrap());
        let mut ranks = vec![0; items.len()];
        let heights = order.iter().map(|&i| items[i].2).collect();
        for (rank, &index) in order.iter().enumerate() {
            ranks[index] = rank;
        }
        order.sort_by(|&a, &b| items[a].1.partial_cmp(&items[b].1).unwrap());
        let mut result = Self {
            nodes: vec![Node::default()],
            roots: vec![0],
            centers: Vec::new(),
            heights,
        };
        for index in order {
            let root = result.insert(*result.roots.last().unwrap(), 0, items.len(), ranks[index]);
            result.roots.push(root);
            result.centers.push(items[index].1);
        }
        result
    }

    /// Only copy the path from the root to the newly added height leaf, sharing all unchanged nodes.
    fn insert(&mut self, previous: usize, low: usize, high: usize, rank: usize) -> usize {
        let mut node = self.nodes[previous];
        node.count += 1;
        if high - low > 1 {
            let middle = (low + high) / 2;
            if rank < middle {
                node.left = self.insert(node.left, low, middle, rank);
            } else {
                node.right = self.insert(node.right, middle, high, rank);
            }
        }
        self.nodes.push(node);
        self.nodes.len() - 1
    }

    /// Find the zero-based position in the ordered multiple set of the left prefix plus the complete set minus the right prefix.
    fn select(&self, mut left: usize, mut total: usize, mut right: usize, mut rank: usize) -> f64 {
        let (mut low, mut high) = (0, self.heights.len());
        while high - low > 1 {
            let a = self.nodes[left];
            let b = self.nodes[total];
            let c = self.nodes[right];
            let count =
                self.nodes[a.left].count + self.nodes[b.left].count - self.nodes[c.left].count;
            let middle = (low + high) / 2;
            if rank < count {
                left = a.left;
                total = b.left;
                right = c.left;
                high = middle;
            } else {
                rank -= count;
                left = a.right;
                total = b.right;
                right = c.right;
                low = middle;
            }
        }
        self.heights[low]
    }

    /// After longitudinal exclusion of closed intervals, one or two median values of the highest quartile are accurately read.
    pub fn height(&self, top: f64, bottom: f64, fallback: f64) -> Option<f64> {
        if !top.is_finite() || !bottom.is_finite() || top > bottom || !fallback.is_finite() {
            return None;
        }
        let left = self.centers.partition_point(|&y| y < top);
        let right = self.centers.partition_point(|&y| y <= bottom);
        let n = left + self.centers.len() - right;
        if n < 4 {
            return Some(fallback);
        }
        let count = n.div_ceil(4);
        let rank = n - count + count / 2;
        let a = self.select(
            self.roots[left],
            *self.roots.last().unwrap(),
            self.roots[right],
            rank,
        );
        let result = if count % 2 == 1 {
            a
        } else {
            let b = self.select(
                self.roots[left],
                *self.roots.last().unwrap(),
                self.roots[right],
                rank - 1,
            );
            (b + a) / 2.0
        };
        result.is_finite().then_some(result)
    }
}

pub struct CoreRows {
    pub members: Vec<Vec<i64>>,
    size: usize,
    extents: Vec<(f64, f64)>,
}

impl CoreRows {
    /// All centers from the same source are included in the row range to avoid mistaken omission when repeated sources fall outside the exclusion interval.
    pub fn new(
        members: Vec<Vec<i64>>,
        extents: &std::collections::HashMap<i64, (f64, f64)>,
    ) -> Self {
        let size = members.len().max(1).next_power_of_two();
        let mut tree = vec![(f64::INFINITY, f64::NEG_INFINITY); size * 2];
        for (row, ids) in members.iter().enumerate() {
            for id in ids {
                if let Some(&(a, b)) = extents.get(id) {
                    tree[size + row].0 = tree[size + row].0.min(a);
                    tree[size + row].1 = tree[size + row].1.max(b);
                }
            }
        }
        for i in (1..size).rev() {
            tree[i] = (
                tree[i * 2].0.min(tree[i * 2 + 1].0),
                tree[i * 2].1.max(tree[i * 2 + 1].1),
            );
        }
        Self {
            members,
            size,
            extents: tree,
        }
    }

    /// Query the central range of all core sources in consecutive rows without transmitting or expanding the member list.
    pub fn contained(&self, start: usize, end: usize, top: f64, bottom: f64) -> bool {
        let (mut l, mut r) = (start + self.size, end + self.size);
        while l < r {
            if l % 2 == 1 {
                if self.extents[l].0 < top || self.extents[l].1 > bottom {
                    return false;
                }
                l += 1;
            }
            if r % 2 == 1 {
                r -= 1;
                if self.extents[r].0 < top || self.extents[r].1 > bottom {
                    return false;
                }
            }
            l /= 2;
            r /= 2;
        }
        true
    }
}
