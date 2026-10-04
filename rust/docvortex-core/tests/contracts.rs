//! The independent geometric results are used to constrain the kernel boundary, and the difference between Python random and real PDF is performed separately.

use docvortex_core::{dedup, scripts, tables};

/// The first row without internal vertical partitions shall form a rectangular cell spanning two columns.
#[test]
fn merged_cell_is_rectangular() {
    let parents = tables::grid_parents(4, vec![(0, 1)]);
    let (_, specs) = tables::component_specs(parents, 2, 2);
    assert_eq!(specs, Some(vec![(0, 0, 1, 2), (1, 0, 1, 1), (1, 1, 1, 1)]));
}

/// L-shaped connected components cannot be turned into a rectangular table that swallows spaces.
#[test]
fn l_shaped_component_is_rejected() {
    let parents = tables::grid_parents(4, vec![(0, 1), (0, 2)]);
    assert_eq!(tables::component_specs(parents, 2, 2).1, None);
}

/// The reverse line segment and the connecting line segment jointly cover the track, and the zero-length target maintains zero coverage.
#[test]
fn reversed_and_touching_intervals() {
    let values = tables::coverage_batch(
        vec![(0, 1.0, 5.0, 0.0), (0, 1.0, 5.0, 10.0)],
        vec![
            (0, vec![1.0], 0.0, 10.0, 0.0),
            (0, vec![1.0], 2.0, 2.0, 0.0),
        ],
    );
    assert_eq!(values, Some(vec![1.0, 0.0]));
}

/// After the stack reaches the upper limit of protection, subsequent glyphs must no longer generate deletion candidates.
#[test]
fn paint_bucket_limit_preserves_later_glyphs() {
    let records = (0..80)
        .map(|i| (Some([0.0, 0.0, 5.0, 10.0]), Some([0.0, 9.0]), 0, i, true))
        .collect();
    let (pairs, offsets) = dedup::paint_pairs(records).unwrap();
    assert_eq!(pairs.len(), 64 * 63 / 2);
    assert!(pairs.iter().all(|p| p.1 < 64));
    assert!(offsets.is_empty());
}

/// The upward-moving numbers of the trumpet after the two stable text letters should become superscripts.
#[test]
fn raised_digit_follows_body_baseline() {
    let records = vec![
        (
            [0.0, 0.0, 5.0, 10.0],
            Some([0.0, 1.0, 5.0, 9.0]),
            Some([0.0, 9.0]),
            4 | 16 | 256,
            0,
        ),
        (
            [5.0, 0.0, 10.0, 10.0],
            Some([5.0, 1.0, 10.0, 9.0]),
            Some([5.0, 9.0]),
            4 | 16 | 256,
            0,
        ),
        (
            [10.0, -2.0, 13.0, 4.0],
            Some([10.0, -1.0, 13.0, 3.0]),
            Some([10.0, 3.0]),
            4 | 8 | 16 | 256,
            0,
        ),
    ];
    assert_eq!(scripts::classify(records), Some(vec![0, 0, 1]));
}

/// When ordinary boxes are not adjacent, the original line boxes can still retain continuous character paths.
#[test]
fn baseline_keeps_original_source_branch() {
    let index = docvortex_core::spatial::BaselineGeometry::new(
        vec![(0.0, 12.5), (0.0, 12.5)],
        vec![0, 0],
        vec![[0.0, 0.0, 1.0, 1.0], [50.0, 0.0, 51.0, 1.0]],
        vec![1.0, 1.0],
        vec![Some([0.0, 0.0, 10.0, 10.0]), Some([10.0, 0.0, 20.0, 10.0])],
    )
    .unwrap();
    assert_eq!(index.rows(0, 64, 8192), vec![vec![1], vec![]]);
}

/// In the competition for equal points, the first small row and the first subject must be selected and will not change with the sorting implementation.
#[test]
fn inline_ties_are_stable() {
    let a = ([10.0, 0.0, 12.0, 4.0], 4.0, 4.0, 1, false, 0, 0);
    let b = ([0.0, 2.0, 10.0, 12.0], 10.0, 10.0, 1, false, 1, 0);
    assert_eq!(
        docvortex_core::inline_pairs::matches(vec![a, b, a, b]),
        Some(vec![(0, 1, false, false)])
    );
}

/// Aggregation draw selects the first coordinate source, and repeated sources do not change the output member order.
#[test]
fn annotation_union_preserves_first_coordinate() {
    let index = docvortex_core::annotation_geometry::AnnotationGeometry::new(vec![
        (8, [-0.0, 0.0, 1.0, 1.0], [-0.0, 0.0, 1.0, 1.0]),
        (8, [0.0, 0.0, 2.0, 1.0], [0.0, 0.0, 2.0, 1.0]),
        (3, [0.0, 0.0, 2.0, 1.0], [0.0, 0.0, 2.0, 1.0]),
    ])
    .unwrap();
    let (lines, bbox) = index.aggregate(vec![0, 1, 2], vec![], None).unwrap();
    assert_eq!(lines, vec![(8, [0, 0, 1, 0], 2), (3, [2, 2, 2, 2], 1)]);
    assert_eq!(bbox, Some([0, 0, 1, 0]));
    assert!(index.aggregate(vec![3], vec![], None).is_none());
}
