//! 用独立几何结果约束内核边界，Python 随机与真实 PDF 差分另行执行。

use docvortex_core::{dedup, scripts, tables};

/// 无内部竖隔断的第一行应构成横跨两列的矩形单元格。
#[test]
fn merged_cell_is_rectangular() {
    let parents = tables::grid_parents(4, vec![(0, 1)]);
    let (_, specs) = tables::component_specs(parents, 2, 2);
    assert_eq!(specs, Some(vec![(0, 0, 1, 2), (1, 0, 1, 1), (1, 1, 1, 1)]));
}

/// L 形连通分量不能变成吞并空格的矩形表格。
#[test]
fn l_shaped_component_is_rejected() {
    let parents = tables::grid_parents(4, vec![(0, 1), (0, 2)]);
    assert_eq!(tables::component_specs(parents, 2, 2).1, None);
}

/// 反向线段与相接线段共同覆盖轨道，零长目标保持零覆盖。
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

/// 叠层达到保护上限后，后续字形不得再产生删除候选。
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

/// 两个稳定正文字母后的小号上移数字应成为上标。
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
