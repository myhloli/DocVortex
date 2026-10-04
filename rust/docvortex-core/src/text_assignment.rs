//! Continuously execute tight-first, punctuation bridging and blank attribution on own text data without relying on Python/PDFium.
use crate::geometry::Box4;
use std::collections::HashMap;

pub const SPACE: u8 = 1;
pub const PUNCTUATION: u8 = 2;
pub const STOP: u8 = 4;
pub const START: u8 = 8;
pub const BREAK: u8 = 16;

pub struct Character {
    pub bbox: Box4,
    pub tight: Option<Box4>,
    pub flags: u8,
}

/// Consistent with the limited bbox checksum of Python, whitespace characters are additionally allowed for zero area advance point.
fn valid(b: Box4, zero: bool) -> bool {
    b.iter().all(|value| value.is_finite())
        && if zero {
            b[2] >= b[0] && b[3] >= b[1]
        } else {
            b[2] > b[0] && b[3] > b[1]
        }
}

/// Keep the original central axis threshold and starting and ending punctuation boundary rules, and double punctuation will be processed first according to the end-of-line rules.
fn inside(b: Box4, span: Box4, flags: u8, ratio: f64) -> bool {
    let x = (b[0] + b[2]) / 2.0;
    let y = (b[1] + b[3]) / 2.0;
    let middle = (span[1] + span[3]) / 2.0;
    let height = span[3] - span[1];
    if !(span[1] < y && y < span[3] && (y - middle).abs() < height * ratio) {
        return false;
    }
    if span[0] < x && x < span[2] {
        return true;
    }
    if flags & STOP != 0 {
        span[2] - height < b[0] && b[0] < span[2] && x > span[0]
    } else if flags & START != 0 {
        span[0] < b[2] && b[2] < span[0] + height && x < span[2]
    } else {
        false
    }
}

/// Retain the stable ranking of the normalized vertical/horizontal center distance and the original span order.
fn rank(b: Box4, span: Box4, index: usize) -> (f64, f64, usize) {
    let width = (span[2] - span[0]).max(1e-6);
    let height = (span[3] - span[1]).max(1e-6);
    (
        (((b[1] + b[3]) / 2.0) - ((span[1] + span[3]) / 2.0)).abs() / height,
        (((b[0] + b[2]) / 2.0) - ((span[0] + span[2]) / 2.0)).abs() / width,
        index,
    )
}

struct Grid {
    size: f64,
    ratio: f64,
    ranges: Vec<(f64, f64)>,
    cells: Option<HashMap<i64, Vec<usize>>>,
}

impl Grid {
    /// Common ranges use grids; huge ranges are changed to equivalent interval scans without truncating any candidates.
    fn new(spans: &[Box4], size: f64, ratio: f64) -> Self {
        let ranges: Vec<_> = spans
            .iter()
            .map(|b| ((b[1] / size).trunc(), (b[3] / size).trunc()))
            .collect();
        let count: f64 = ranges.iter().map(|(a, b)| (b - a + 1.0).max(0.0)).sum();
        let ordinary = count <= 1_000_000.0
            && ranges
                .iter()
                .all(|(a, b)| a.abs() < i64::MAX as f64 && b.abs() < i64::MAX as f64);
        let cells = ordinary.then(|| {
            let mut cells: HashMap<i64, Vec<usize>> = HashMap::new();
            for (index, (a, b)) in ranges.iter().enumerate() {
                for cell in *a as i64..=*b as i64 {
                    cells.entry(cell).or_default().push(index);
                }
            }
            cells
        });
        Self {
            size,
            ratio,
            ranges,
            cells,
        }
    }

    /// The candidate order maintains the span input order, and blanks are assigned by the first hit rather than the minimum distance.
    fn matching(&self, b: Box4, flags: u8, spans: &[Box4], first: bool) -> Option<usize> {
        let cell = (((b[1] + b[3]) / 2.0) / self.size).trunc();
        let x = (b[0] + b[2]) / 2.0;
        let mut best = None;
        let mut visit = |index: usize| {
            let span = spans[index];
            if flags & (STOP | START) == 0 && !(span[0] < x && x < span[2]) {
                return false;
            }
            if inside(b, span, flags, self.ratio) {
                if first {
                    best = Some(index);
                    return true;
                }
                if best.is_none_or(|old| rank(b, span, index) < rank(b, spans[old], old)) {
                    best = Some(index);
                }
            }
            false
        };
        if let Some(cells) = &self.cells {
            if cell.abs() < i64::MAX as f64 {
                for &index in cells.get(&(cell as i64)).into_iter().flatten() {
                    if visit(index) {
                        break;
                    }
                }
            }
        } else {
            for (index, &(a, b)) in self.ranges.iter().enumerate() {
                if a <= cell && cell <= b && visit(index) {
                    break;
                }
            }
        }
        best
    }
}

/// Complete visible character matching, line break isolation, punctuation filling and blank repair at one time, and output the span index of the original character sequence.
pub fn assign(
    chars: &[Character],
    spans: &[Box4],
    median_height: f64,
    ratio: f64,
) -> Vec<Option<usize>> {
    let grid = Grid::new(spans, median_height.max(1.0), ratio);
    let mut assigned = vec![None; chars.len()];
    for (index, ch) in chars.iter().enumerate() {
        if ch.flags & SPACE != 0 {
            continue;
        }
        for bbox in [ch.tight, Some(ch.bbox)].into_iter().flatten() {
            if valid(bbox, false) {
                assigned[index] = grid.matching(bbox, ch.flags, spans, false);
                if assigned[index].is_some() {
                    break;
                }
            }
        }
    }
    let mut previous = vec![None; chars.len()];
    let mut next = vec![None; chars.len()];
    let mut owner = None;
    for (index, ch) in chars.iter().enumerate() {
        if ch.flags & BREAK != 0 {
            owner = None;
            continue;
        }
        previous[index] = owner;
        if ch.flags & SPACE == 0 && assigned[index].is_some() {
            owner = assigned[index];
        }
    }
    owner = None;
    for (index, ch) in chars.iter().enumerate().rev() {
        if ch.flags & BREAK != 0 {
            owner = None;
            continue;
        }
        next[index] = owner;
        if ch.flags & SPACE == 0 && assigned[index].is_some() {
            owner = assigned[index];
        }
    }
    for (index, ch) in chars.iter().enumerate() {
        if assigned[index].is_some() || ch.flags & PUNCTUATION == 0 {
            continue;
        }
        if let Some(owner) = previous[index].filter(|owner| next[index] == Some(*owner)) {
            let span = spans[owner];
            if [ch.tight, Some(ch.bbox)].into_iter().flatten().any(|bbox| {
                let x = (bbox[0] + bbox[2]) / 2.0;
                let y = (bbox[1] + bbox[3]) / 2.0;
                valid(bbox, false) && span[0] <= x && x <= span[2] && span[1] <= y && y <= span[3]
            }) {
                assigned[index] = Some(owner);
            }
        }
    }
    for (index, ch) in chars.iter().enumerate() {
        if ch.flags & SPACE == 0 || !valid(ch.bbox, true) {
            continue;
        }
        let same = previous[index].is_some() && previous[index] == next[index];
        let neighbor = if same {
            previous[index]
        } else {
            match (previous[index], next[index]) {
                (owner, None) | (None, owner) => owner,
                _ => None,
            }
        };
        assigned[index] = if ch.flags & BREAK == 0
            && neighbor.is_some_and(|owner| same || inside(ch.bbox, spans[owner], ch.flags, ratio))
        {
            neighbor
        } else {
            grid.matching(ch.bbox, ch.flags, spans, true)
        };
    }
    assigned
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Construct real geometric inputs that do not rely on PDFium, testing belonging boundaries rather than binding implementations.
    fn character(bbox: Box4, flags: u8) -> Character {
        Character {
            bbox,
            tight: None,
            flags,
        }
    }

    #[test]
    fn tight_precedes_loose_and_ties_keep_span_order() {
        // When tight hits the second box, it must not be covered by the first box of loose; the input sequence is retained when completely overlapping.
        let mut chars = vec![character([2.0, 2.0, 4.0, 8.0], 0)];
        chars[0].tight = Some([12.0, 2.0, 14.0, 8.0]);
        let spans = [[0.0, 0.0, 10.0, 10.0], [10.0, 0.0, 20.0, 10.0]];
        assert_eq!(assign(&chars, &spans, 10.0, 0.33), [Some(1)]);
        assert_eq!(assign(&chars, &[spans[1], spans[1]], 10.0, 0.33), [Some(0)]);
    }

    #[test]
    fn neighbor_spaces_and_punctuation_do_not_cross_breaks() {
        // Bridge when the punctuation point exceeds the central axis zone but the center is still within the same frame; line breaks prevent neighbor attribution propagation.
        let span = [[0.0, 0.0, 10.0, 10.0]];
        let chars = [
            character([1.0, 2.0, 2.0, 8.0], 0),
            character([3.0, 0.1, 4.0, 0.3], PUNCTUATION),
            character([4.0, 0.0, 4.0, 0.0], SPACE),
            character([5.0, 2.0, 6.0, 8.0], 0),
            character([0.0, -10.0, 0.0, -10.0], SPACE | BREAK),
            character([7.0, 0.0, 7.0, 0.0], SPACE),
        ];
        assert_eq!(
            assign(&chars, &span, 10.0, 0.33),
            [Some(0), Some(0), Some(0), Some(0), None, None]
        );
    }

    #[test]
    fn whitespace_uses_first_match_instead_of_nearest() {
        // No-neighbor blanks are in original candidate order, while ordinary characters use the nearest center.
        let spans = [[0.0, 0.0, 10.0, 20.0], [0.0, 5.0, 10.0, 15.0]];
        let spaces = [character([4.0, 7.0, 5.0, 9.0], SPACE)];
        assert_eq!(assign(&spaces, &spans, 10.0, 0.33), [Some(0)]);
    }

    #[test]
    fn sparse_grid_preserves_large_range_candidates() {
        // The huge grid range uses equivalent scanning, and still hits the full coverage span, without cropping according to the number of candidates.
        let spans = [[0.0, 0.0, 10.0, 100_000_000.0]];
        let chars = [character([3.0, 49_999_999.0, 4.0, 50_000_001.0], 0)];
        assert_eq!(assign(&chars, &spans, 1.0, 0.33), [Some(0)]);
    }
}
