//! 连续生成全文字体 run 统计，保留样本成员、相邻对和同族传播顺序。
use crate::{
    geometry::{anchor_pairs, Box4, Size},
    geometry_risk::quantile,
    median,
};
use std::collections::HashSet;

pub struct Sample {
    pub run: usize,
    pub position: i64,
    pub source: Box4,
    pub tight: Box4,
    pub origin: Size,
    pub size: f64,
    pub anchor: bool,
}

#[derive(Default)]
pub struct Run {
    pub members: Vec<usize>,
    pub ratios: Vec<f64>,
    pub overlaps: Vec<bool>,
    pub advances: Vec<f64>,
    pub bearings: Vec<f64>,
    pub median_advance: Option<f64>,
    pub median_bearing: f64,
    pub strong: bool,
    pub sibling: bool,
}

/// 一次处理所有行的锚点和 run，返回最终统计；非有限或越界输入明确拒绝。
pub fn build(
    samples: Vec<Sample>,
    lines: Vec<Vec<usize>>,
    families: Vec<usize>,
) -> Option<Vec<Run>> {
    if samples.iter().any(|s| {
        s.run >= families.len()
            || !s.size.is_finite()
            || s.source
                .iter()
                .chain(&s.tight)
                .chain(&s.origin)
                .any(|v| !v.is_finite())
    }) || lines.iter().flatten().any(|i| *i >= samples.len())
    {
        return None;
    }
    let mut runs: Vec<Run> = (0..families.len()).map(|_| Run::default()).collect();
    for (index, sample) in samples.iter().enumerate() {
        let bearing = sample.tight[0] - sample.origin[0];
        if !bearing.is_finite() {
            return None;
        }
        runs[sample.run].members.push(index);
        runs[sample.run].bearings.push(bearing);
    }
    for line in lines {
        let mut anchors: Vec<_> = line.into_iter().filter(|i| samples[*i].anchor).collect();
        anchors.sort_by_key(|i| samples[*i].position);
        let rows = anchors
            .iter()
            .map(|i| {
                let s = &samples[*i];
                (s.run, s.source, s.tight, s.origin, s.size)
            })
            .collect();
        for (index, advance, ratio, overlap) in anchor_pairs(rows, true)? {
            let run = &mut runs[samples[anchors[index]].run];
            run.ratios.push(ratio);
            run.advances.push(advance);
            run.overlaps.push(overlap);
        }
    }
    for run in &mut runs {
        run.median_advance = (!run.advances.is_empty()).then(|| median(run.advances.clone()));
        run.median_bearing = median(run.bearings.clone());
        let count = run.ratios.len();
        if count < 30 {
            continue;
        }
        run.strong = median(run.ratios.clone()) >= 1.30
            && run.ratios.iter().filter(|v| **v > 1.35).count() as f64 / count as f64 >= 0.30
            && run.overlaps.iter().filter(|v| **v).count() as f64 / count as f64 >= 0.30;
    }
    let bad: HashSet<_> = runs
        .iter()
        .enumerate()
        .filter_map(|(i, r)| r.strong.then_some(families[i]))
        .collect();
    for (i, run) in runs.iter_mut().enumerate() {
        if run.strong || !bad.contains(&families[i]) || run.ratios.len() < 10 {
            continue;
        }
        run.sibling = (median(run.ratios.clone()) >= 1.15
            && run.overlaps.iter().filter(|v| **v).count() as f64 / run.overlaps.len() as f64
                >= 0.50)
            || quantile(run.ratios.clone(), 0.9) >= 1.50;
    }
    Some(runs)
}
