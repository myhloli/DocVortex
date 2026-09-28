//! 在自有字符上完成正文片段组装；Unicode 分类和 NFC 组合由绑定层按解释器语义准备。
use crate::{geometry::Box4, text_snapshot::TextSnapshot};
use std::collections::{HashMap, HashSet};

pub struct Content {
    pub text: Option<String>,
    pub private_count: usize,
    pub text_count: usize,
    pub private_run: usize,
}

/// 按原字符顺序收集各 span 的成员，保持 PUA 连续段判定所需的原始顺序。
pub fn groups(assignments: &[Option<usize>], span_count: usize) -> Vec<Vec<usize>> {
    let mut output = vec![Vec::new(); span_count];
    for (index, owner) in assignments.iter().enumerate() {
        if let Some(owner) = owner {
            output[*owner].push(index);
        }
    }
    output
}

/// 与 Python 一样，仅在来源编号乱序时稳定排序，等号不改变成员顺序。
pub fn ordered(data: &TextSnapshot, group: &[usize]) -> Vec<usize> {
    let mut values = group.to_vec();
    if values
        .windows(2)
        .any(|pair| data.chars[pair[0]].index > data.chars[pair[1]].index)
    {
        values.sort_by_key(|&index| data.chars[index].index);
    }
    values
}

/// 计算相对较短边的交集比例，零宽高不组成重叠附加符。
fn overlap(a: Box4, b: Box4, axis: usize) -> f64 {
    let denominator = (a[axis + 2] - a[axis]).min(b[axis + 2] - b[axis]);
    if denominator <= 0.0 {
        return 0.0;
    }
    (a[axis + 2].min(b[axis + 2]) - a[axis].max(b[axis])).max(0.0) / denominator
}

/// 保留当前字符为附加符时的优先分支；不在失败后尝试参考实现未覆盖的反向组合。
pub fn composition_pair(
    data: &TextSnapshot,
    first: usize,
    second: usize,
    modifiers: &HashMap<String, String>,
    threshold: f64,
) -> Option<(usize, usize)> {
    let (base, modifier) = if modifiers.contains_key(&data.chars[first].text) {
        (second, first)
    } else if modifiers.contains_key(&data.chars[second].text) {
        (first, second)
    } else {
        return None;
    };
    let a = &data.chars[base];
    let b = &data.chars[modifier];
    (a.text.chars().count() == 1
        && overlap(a.bbox, b.bbox, 0) >= threshold
        && overlap(a.bbox, b.bbox, 1) >= threshold)
        .then_some((base, modifier))
}

/// 仅替换现有控制字符和七种拉丁连字，不把其他 Unicode 字符擅自做 NFKC 转换。
fn clean(text: &str) -> String {
    let controls = text.replace("\r\n", "").replace('\u{2}', "-");
    let mut output = String::with_capacity(controls.len());
    for ch in controls.chars() {
        match ch {
            '\u{fb01}' => output.push_str("fi"),
            '\u{fb02}' => output.push_str("fl"),
            '\u{fb00}' => output.push_str("ff"),
            '\u{fb03}' => output.push_str("ffi"),
            '\u{fb04}' => output.push_str("ffl"),
            '\u{fb05}' => output.push_str("ft"),
            '\u{fb06}' => output.push_str("st"),
            _ => output.push(ch),
        }
    }
    output
}

/// 连续执行 PUA 信号、重叠附加符合成、词间空格及正文组装；空组保留调用方原文本。
pub struct Rules<'a> {
    pub ordinary: &'a HashSet<char>,
    pub decimals: &'a HashSet<char>,
    pub tight_spacing: &'a [bool],
    pub modifiers: &'a HashMap<String, String>,
    pub threshold: f64,
    pub compositions: &'a HashMap<(String, String), String>,
    pub spaces: &'a HashSet<char>,
    pub breaks: &'a HashSet<String>,
    pub private_range: (u32, u32),
}

/// 使用本次调用固定的字符规则构造所有片段，避免重新读取可变 Python 配置。
pub fn build(data: &TextSnapshot, groups: &[Vec<usize>], rules: &Rules<'_>) -> Vec<Content> {
    let Rules {
        ordinary,
        decimals,
        tight_spacing,
        modifiers,
        threshold,
        compositions,
        spaces,
        breaks,
        private_range,
    } = rules;
    groups
        .iter()
        .enumerate()
        .map(|(group_index, group)| {
            let mut result = Content {
                text: None,
                private_count: 0,
                text_count: 0,
                private_run: 0,
            };
            let mut run = 0;
            for &index in group {
                for ch in data.chars[index].text.chars() {
                    if spaces.contains(&ch) {
                        run = 0;
                        continue;
                    }
                    result.text_count += 1;
                    if private_range.0 <= ch as u32 && ch as u32 <= private_range.1 {
                        result.private_count += 1;
                        run += 1;
                        result.private_run = result.private_run.max(run);
                    } else {
                        run = 0;
                    }
                }
            }
            if group.is_empty() {
                return result;
            }
            let members = ordered(data, group);
            let mut chars: Vec<(&str, Box4, usize)> = Vec::with_capacity(members.len());
            let mut cursor = 0;
            while cursor < members.len() {
                if cursor + 1 < members.len() {
                    if let Some((base, modifier)) = composition_pair(
                        data,
                        members[cursor],
                        members[cursor + 1],
                        modifiers,
                        *threshold,
                    ) {
                        let a = &data.chars[base];
                        let b = &data.chars[modifier];
                        if let Some(composed) = compositions.get(&(a.text.clone(), b.text.clone()))
                        {
                            chars.push((composed, a.bbox, base));
                            cursor += 2;
                            continue;
                        }
                    }
                }
                let ch = &data.chars[members[cursor]];
                chars.push((&ch.text, ch.bbox, members[cursor]));
                cursor += 1;
            }
            let mut widths: Vec<_> = chars.iter().map(|(_, b, _)| b[2] - b[0]).collect();
            widths.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
            let median = if widths.len() % 2 == 1 {
                widths[widths.len() / 2]
            } else {
                (widths[widths.len() / 2 - 1] + widths[widths.len() / 2]) / 2.0
            };
            let mut text = String::new();
            for (index, &(value, bbox, source)) in chars.iter().enumerate() {
                if breaks.contains(value) {
                    continue;
                }
                text.push_str(value);
                if let Some(&(next, next_box, next_source)) = chars.get(index + 1) {
                    if (next_box[0] - bbox[2] > median * 0.25
                        || (tight_spacing.get(group_index).copied().unwrap_or(false)
                            && crate::text_spacing::needs_space(
                                data,
                                source,
                                next_source,
                                ordinary,
                                decimals,
                            )))
                        && value != " "
                        && next != " "
                    {
                        text.push(' ');
                    }
                }
            }
            result.text = Some(clean(&text));
            result
        })
        .collect()
}
