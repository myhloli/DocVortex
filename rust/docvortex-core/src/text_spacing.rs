//! 与 Python 参考实现相同的相邻非 CJK 字形词界判定。
use crate::{geometry::Box4, text_snapshot::TextSnapshot};
use std::collections::HashSet;

/// 排除汉字扩展、假名、韩文及兼容字形，不以所在整行的语言决定词界。
pub fn is_cjk(ch: char) -> bool {
    matches!(ch as u32,
        0x1100..=0x11ff | 0x2e80..=0xa4cf | 0xa960..=0xa97f | 0xac00..=0xd7ff |
        0xf900..=0xfaff | 0xff66..=0xff9f | 0x1aff0..=0x1b16f | 0x20000..=0x323af)
}

/// 墨迹框只转换坐标轴；相邻间隙与页面原点平移无关。
fn local(b: Box4, angle: i32) -> Box4 {
    match angle {
        90 => [b[1], -b[2], b[3], -b[0]],
        180 => [-b[2], -b[3], -b[0], -b[1]],
        270 => [-b[3], b[0], -b[1], b[2]],
        _ => b,
    }
}

/// 只接受解释器确认的普通单字形，避免两端标点或连字展开改变语义。
fn ordinary(text: &str, letters: &HashSet<char>) -> bool {
    let mut chars = text.chars();
    let Some(ch) = chars.next() else { return false };
    chars.next().is_none() && !is_cjk(ch) && (ch.is_ascii_alphanumeric() || letters.contains(&ch))
}

/// 使用真实源编号、方向和基线保护，只为可靠的大墨迹间隙返回一个词界。
pub fn needs_space(
    data: &TextSnapshot,
    first: usize,
    second: usize,
    letters: &HashSet<char>,
    decimals: &HashSet<char>,
) -> bool {
    let a = &data.chars[first];
    let b = &data.chars[second];
    if !ordinary(&a.text, letters)
        || !ordinary(&b.text, letters)
        || a.index.checked_add(1) != Some(b.index)
    {
        return false;
    }
    if a.text
        .chars()
        .all(|c| c.is_ascii_digit() || decimals.contains(&c))
        && b.text
            .chars()
            .all(|c| c.is_ascii_digit() || decimals.contains(&c))
    {
        return false;
    }
    for font in [&data.fonts[a.font], &data.fonts[b.font]] {
        let name = font.name.to_lowercase();
        if font.flags & 1 != 0
            || [
                "mono",
                "courier",
                "consolas",
                "menlo",
                "typewriter",
                "fixed",
            ]
            .iter()
            .any(|hint| name.contains(hint))
        {
            return false;
        }
    }
    let ls = data.fonts[a.font].size;
    let rs = data.fonts[b.font].size;
    let em = ls.max(rs);
    if !ls.is_finite() || !rs.is_finite() || ls.min(rs) <= 0.0 || ls.min(rs) < 0.8 * em {
        return false;
    }
    if !a.writing_angle.is_finite() || !b.writing_angle.is_finite() {
        return false;
    }
    let degrees = a.writing_angle.to_degrees().rem_euclid(360.0);
    let angle = ((degrees / 90.0).round_ties_even() as i32 * 90) % 360;
    if ((degrees - angle as f64 + 180.0).rem_euclid(360.0) - 180.0).abs() > 0.1
        || ((b.writing_angle.to_degrees() - angle as f64 + 180.0).rem_euclid(360.0) - 180.0).abs()
            > 0.1
    {
        return false;
    }
    let (Some(ab), Some(bb)) = (a.tight, b.tight) else {
        return false;
    };
    if ab.iter().chain(bb.iter()).any(|v| !v.is_finite()) {
        return false;
    }
    let ab = local(ab, angle);
    let bb = local(bb, angle);
    if ab[2] <= ab[0]
        || bb[2] <= bb[0]
        || ab[3] <= ab[1]
        || bb[3] <= bb[1]
        || ab[3] - ab[1] > 1.5 * ls
        || bb[3] - bb[1] > 1.5 * rs
        || bb[0] - ab[2] <= 0.25 * em
        || ab[3].min(bb[3]) - ab[1].max(bb[1]) < 0.5 * (ab[3] - ab[1]).min(bb[3] - bb[1])
    {
        return false;
    }
    if let (Some(ao), Some(bo)) = (a.origin, b.origin) {
        let axis = usize::from(angle != 90 && angle != 270);
        if ao.iter().chain(bo.iter()).any(|v| !v.is_finite())
            || (ao[axis] - bo[axis]).abs() > 0.15 * em
        {
            return false;
        }
    } else if (ab[3] - bb[3]).abs() > 0.15 * em {
        return false;
    }
    true
}
