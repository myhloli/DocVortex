//! Order-preserving row frame aggregation and maximum lower boundary query return the source position to share the original coordinates of Python.

pub struct RowGeometry {
    boxes: Vec<[f64; 4]>,
    size: usize,
    tree: Vec<[usize; 4]>,
}

impl RowGeometry {
    /// Non-finite geometries are rejected, and the earlier source is selected to avoid changing negative zero and original coordinate identity.
    pub fn new(boxes: Vec<[f64; 4]>) -> Option<Self> {
        if boxes.iter().flatten().any(|x| !x.is_finite()) {
            return None;
        }
        let size = boxes.len().max(1).next_power_of_two();
        let mut result = Self {
            boxes,
            size,
            tree: vec![[usize::MAX; 4]; size * 2],
        };
        for i in 0..result.boxes.len() {
            result.tree[size + i] = [i; 4];
        }
        for i in (1..size).rev() {
            result.tree[i] = result.combine(result.tree[i * 2], result.tree[i * 2 + 1]);
        }
        Some(result)
    }

    /// Merge the extreme value sources of the order-preserving interval, and strictly compare the equal values to always retain the left object.
    fn combine(&self, a: [usize; 4], b: [usize; 4]) -> [usize; 4] {
        let mut output = a;
        for k in 0..4 {
            if a[k] == usize::MAX {
                output[k] = b[k];
            } else if b[k] != usize::MAX {
                let better = if k < 2 {
                    self.boxes[b[k]][k] < self.boxes[a[k]][k]
                } else {
                    self.boxes[b[k]][k] > self.boxes[a[k]][k]
                };
                if better {
                    output[k] = b[k];
                }
            }
        }
        output
    }

    /// Using left and right independent accumulators, query tree splitting cannot change the comparison order of the original rows.
    pub fn union_indices(&self, start: usize, end: usize) -> Option<[usize; 4]> {
        if start >= end || end > self.boxes.len() {
            return None;
        }
        let (mut l, mut r) = (self.size + start, self.size + end);
        let (mut left, mut right) = ([usize::MAX; 4], [usize::MAX; 4]);
        while l < r {
            if l % 2 == 1 {
                left = self.combine(left, self.tree[l]);
                l += 1;
            }
            if r % 2 == 1 {
                r -= 1;
                right = self.combine(self.tree[r], right);
            }
            l /= 2;
            r /= 2;
        }
        Some(self.combine(left, right))
    }

    /// Find the first row in the original sequence whose lower boundary strictly exceeds the bottom of the table, and the boundary is not required to be monotonous.
    pub fn first_after(&self, bottom: f64) -> usize {
        if self.boxes.is_empty() {
            return 0;
        }
        let mut node = 1;
        let source = self.tree[node][3];
        if self.boxes[source][3] <= bottom {
            return self.boxes.len();
        }
        while node < self.size {
            let left = self.tree[node * 2][3];
            node = if left != usize::MAX && self.boxes[left][3] > bottom {
                node * 2
            } else {
                node * 2 + 1
            };
        }
        (node - self.size).min(self.boxes.len())
    }
}
