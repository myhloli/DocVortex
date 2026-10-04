//! Own PDF text snapshot; all characters, fonts and sources are Rust data and does not hold Python or PDFium objects.
use crate::{
    dedup,
    geometry::{Box4, Size},
};
use std::collections::{HashMap, HashSet};

#[derive(Clone, Debug)]
pub struct Font {
    pub name: String,
    pub name_id: usize,
    pub flags: i32,
    pub size: f64,
    pub weight: i32,
}
#[derive(Clone, Debug)]
pub struct Character {
    pub text: String,
    pub bbox: Box4,
    pub rotation: f64,
    pub font: usize,
    pub index: usize,
    pub sources: Vec<usize>,
    pub code: u32,
    pub object: Option<usize>,
    pub mode: Option<i32>,
    pub writing_angle: f64,
    pub origin: Option<Size>,
    pub loose: Option<Box4>,
    pub tight: Option<Box4>,
    pub visible: Option<bool>,
}
pub struct InputCharacter {
    pub character: Character,
    pub admitted: bool,
    pub clip: Option<Box4>,
}
#[derive(Clone, Default)]
pub struct Properties {
    pub space: bool,
    pub canonical: String,
    pub han: bool,
}
#[derive(Clone, Copy)]
pub struct Angle {
    pub cos: f64,
    pub sin: f64,
    pub rounded: u64,
    pub bucket: i64,
}
#[derive(Clone)]
pub struct Glyph {
    pub chars: Vec<Character>,
    pub bbox: Option<Box4>,
    pub text: String,
    pub angle: f64,
}
#[derive(Debug)]
pub enum SnapshotError {
    MissingSource(usize),
    Invalid(&'static str),
}
pub struct TextSnapshot {
    pub chars: Vec<Character>,
    pub fonts: Vec<Font>,
    pub extended: bool,
    pub visible_only: bool,
}

/// Judge the interpreter definition blank and do not replace the host semantics with the Rust Unicode version.
pub fn is_space(text: &str, properties: &HashMap<String, Properties>) -> bool {
    !text.is_empty()
        && text
            .chars()
            .all(|ch| properties.get(&ch.to_string()).is_some_and(|p| p.space))
}
/// Strict comparison preserves the first-valued semantics of Python min/max for signed zero.
fn min(a: f64, b: f64) -> f64 {
    if b < a {
        b
    } else {
        a
    }
}
/// Strict comparison maintains the first value semantics of Python and max.
fn max(a: f64, b: f64) -> f64 {
    if b > a {
        b
    } else {
        a
    }
}
/// The visible intersection is calculated without recalculating the character origin or source number.
fn intersection(a: Box4, b: Box4) -> Box4 {
    [
        max(a[0], b[0]),
        max(a[1], b[1]),
        min(a[2], b[2]),
        min(a[3], b[3]),
    ]
}
/// Determine whether the rectangle has a positive area.
fn nonempty(b: Box4) -> bool {
    b[2] > b[0] && b[3] > b[1]
}
/// Only the actual truncated glyphs are clipped, damaging the loose box to continue using the valid ink range.
fn clip_character(
    ch: &mut Character,
    clip: Box4,
    properties: &HashMap<String, Properties>,
) -> bool {
    let ink = ch.tight.unwrap_or(ch.bbox);
    if is_space(&ch.text, properties) {
        let x = (ink[0] + ink[2]) / 2.0;
        let y = (ink[1] + ink[3]) / 2.0;
        return clip[0] <= x && x <= clip[2] && clip[1] <= y && y <= clip[3];
    }
    let visible = intersection(ink, clip);
    if !nonempty(visible) {
        return false;
    }
    if ink != visible {
        let clipped = intersection(ch.bbox, clip);
        ch.bbox = if nonempty(clipped) { clipped } else { visible };
        for value in [&mut ch.loose, &mut ch.tight] {
            if let Some(bbox) = *value {
                let clipped = intersection(bbox, clip);
                *value = Some(if nonempty(clipped) { clipped } else { visible });
            }
        }
    }
    true
}
/// Filtering and assigning writing direction in original order before proxy pair restoration; intra-page object ID has been allocated by binding layer before filtering.
pub fn initialize(
    input: Vec<InputCharacter>,
    properties: &HashMap<String, Properties>,
) -> Vec<Character> {
    let chars: Vec<Character> = input
        .into_iter()
        .filter_map(|item| {
            if !item.admitted {
                return None;
            }
            let mut ch = item.character;
            if item
                .clip
                .is_some_and(|clip| !clip_character(&mut ch, clip, properties))
            {
                return None;
            }
            Some(ch)
        })
        .collect();
    chars
}

pub struct WritingRun {
    start: usize,
    end: usize,
    next: usize,
}
/// Only the continuous same object range with the segment head origin is recorded, and the missing segment head is not replaced with the subsequent origin.
pub fn writing_runs(chars: &[Character]) -> Vec<WritingRun> {
    let mut start = 0;
    let mut runs = Vec::new();
    while start < chars.len() {
        let mut end = start + 1;
        if let Some(object) = chars[start].object {
            while end < chars.len() && chars[end].object == Some(object) {
                end += 1;
            }
            if chars[start].origin.is_some() {
                runs.push(WritingRun {
                    start,
                    end,
                    next: start + 1,
                });
            }
        }
        start = end;
    }
    runs
}
/// Request the next missing direction metadata for each unfinished segment; the cursor avoids degenerating into repeated scans.
pub fn advance_writing_angles(
    chars: &mut [Character],
    runs: &mut [WritingRun],
    directions: &HashMap<(u64, u64), (bool, f64)>,
) -> Vec<(u64, u64)> {
    let mut pending = HashSet::new();
    let mut requested = Vec::new();
    for run in runs {
        let origin = chars[run.start].origin.unwrap();
        while run.next < run.end {
            let Some(next) = chars[run.next].origin else {
                run.next += 1;
                continue;
            };
            let key = (
                (next[0] - origin[0]).to_bits(),
                (next[1] - origin[1]).to_bits(),
            );
            let Some(&(valid, angle)) = directions.get(&key) else {
                if pending.insert(key) {
                    requested.push(key);
                }
                break;
            };
            run.next += 1;
            if valid {
                for ch in &mut chars[run.start..run.end] {
                    ch.writing_angle = angle;
                }
                run.next = run.end;
            }
        }
    }
    requested
}

/// Restore the PDFium UTF-16 proxy pair, keeping the original filtering order, representative characters and sorted source index.
pub fn restore_surrogates(
    chars: Vec<Character>,
    raw_count: usize,
) -> Result<Vec<Character>, SnapshotError> {
    let codes: HashMap<usize, u32> = chars.iter().map(|ch| (ch.index, ch.code)).collect();
    let mut consumed = HashSet::new();
    let mut output = Vec::with_capacity(chars.len());
    for mut ch in chars {
        if consumed.contains(&ch.index) {
            continue;
        }
        if ch.text == "\u{fffd}" && ch.index < raw_count {
            let mut pair = None;
            if (0xd800..=0xdbff).contains(&ch.code) && ch.index + 1 < raw_count {
                let low = *codes
                    .get(&(ch.index + 1))
                    .ok_or(SnapshotError::MissingSource(ch.index + 1))?;
                if (0xdc00..=0xdfff).contains(&low) {
                    consumed.insert(ch.index + 1);
                    pair = Some((ch.code, low, ch.index + 1));
                }
            } else if (0xdc00..=0xdfff).contains(&ch.code) && ch.index > 0 {
                let high = *codes
                    .get(&(ch.index - 1))
                    .ok_or(SnapshotError::MissingSource(ch.index - 1))?;
                if (0xd800..=0xdbff).contains(&high) {
                    pair = Some((high, ch.code, ch.index - 1));
                }
            }
            if let Some((high, low, source)) = pair {
                ch.text = char::from_u32(0x10000 + ((high - 0xd800) << 10) + (low - 0xdc00))
                    .unwrap()
                    .to_string();
                ch.sources.push(source);
                ch.sources.sort_unstable();
                ch.sources.dedup();
            }
        }
        output.push(ch);
    }
    Ok(output)
}
/// Determine the continuous source mapping of the same object and protect the one-to-many output of ligatures.
fn same_mapping(a: &Character, b: &Character, properties: &HashMap<String, Properties>) -> bool {
    a.object.is_some()
        && a.object == b.object
        && a.sources.iter().max().and_then(|i| i.checked_add(1)) == Some(b.index)
        && a.font == b.font
        && a.rotation == b.rotation
        && close(a.origin, b.origin)
        && close(Some(a.bbox), Some(b.bbox))
        && !a.text.is_empty()
        && !b.text.is_empty()
        && !is_space(&a.text, properties)
        && !is_space(&b.text, properties)
}
/// Missing geometry cannot be used as evidence of source equivalence.
fn close<const N: usize>(a: Option<[f64; N]>, b: Option<[f64; N]>) -> bool {
    matches!((a, b), (Some(a), Some(b)) if a.iter().zip(b).all(|(x,y)| x.is_finite() && y.is_finite() && (*x-y).abs() <= 0.001))
}
/// Merge source index but do not change the geometry, font and original code representing the glyph.
fn sources<'a>(chars: impl IntoIterator<Item = &'a Character>) -> Vec<usize> {
    let mut values: Vec<usize> = chars
        .into_iter()
        .flat_map(|ch| ch.sources.iter().copied())
        .collect();
    values.sort_unstable();
    values.dedup();
    values
}
/// Only the actual one-to-many mapping with different texts requires Chinese character normalization, and ordinary characters do not trigger fixed radical resource reading.
pub fn mapping_metadata(
    chars: &[Character],
    properties: &HashMap<String, Properties>,
) -> Vec<String> {
    let mut start = 0;
    let mut output = Vec::new();
    let mut seen = HashSet::new();
    while start < chars.len() {
        let mut end = start + 1;
        while end < chars.len() && same_mapping(&chars[end - 1], &chars[end], properties) {
            end += 1;
        }
        if end > start + 1
            && chars[start + 1..end]
                .iter()
                .any(|ch| ch.text != chars[start].text)
        {
            for ch in &chars[start..end] {
                if seen.insert(ch.text.clone()) {
                    output.push(ch.text.clone());
                }
            }
        }
        start = end;
    }
    output
}
/// Create a complete mapping protection group, folding only single Chinese characters that have been proven equivalent by the host Unicode data.
pub fn mapping_groups(
    chars: Vec<Character>,
    properties: &HashMap<String, Properties>,
) -> Vec<Glyph> {
    let mut groups: Vec<Vec<Character>> = Vec::new();
    for ch in chars {
        if groups
            .last()
            .is_some_and(|group| same_mapping(group.last().unwrap(), &ch, properties))
        {
            groups.last_mut().unwrap().push(ch);
        } else {
            groups.push(vec![ch]);
        }
    }
    groups
        .into_iter()
        .map(|mut group| {
            if group.len() > 1 && group.iter().any(|ch| ch.text != group[0].text) {
                let canonical = properties
                    .get(&group[0].text)
                    .map(|p| p.canonical.as_str())
                    .unwrap_or(&group[0].text);
                if properties.get(canonical).is_some_and(|p| p.han)
                    && group.iter().all(|ch| {
                        properties
                            .get(&ch.text)
                            .map(|p| p.canonical.as_str())
                            .unwrap_or(&ch.text)
                            == canonical
                    })
                {
                    let mut head = group[0].clone();
                    head.text = canonical.to_owned();
                    head.sources = sources(group.iter());
                    group = vec![head];
                }
            }
            let head = &group[0];
            let angle = head.writing_angle;
            let bbox = (angle.is_finite()
                && head.bbox.iter().all(|v| v.is_finite())
                && nonempty(head.bbox))
            .then_some(head.bbox);
            let text = group.iter().map(|ch| ch.text.as_str()).collect();
            Glyph {
                chars: group,
                bbox,
                text,
                angle,
            }
        })
        .collect()
}
/// The sources of repeated glyphs are merged, and all sources of different lengths are attributed to representative characters.
fn merge_sources(retained: &mut Glyph, duplicate: &Glyph) {
    if retained.chars.len() == duplicate.chars.len() {
        for (a, b) in retained.chars.iter_mut().zip(&duplicate.chars) {
            a.sources = sources([&*a, b]);
        }
    } else {
        retained.chars[0].sources = sources(retained.chars.iter().chain(&duplicate.chars));
    }
}
/// Delete the isolated blanks with content on both sides removed, and retain the spaces and line breaks between the text.
fn retained(
    glyphs: Vec<Glyph>,
    removed: &HashSet<usize>,
    properties: &HashMap<String, Properties>,
) -> Vec<Glyph> {
    if removed.is_empty() {
        return glyphs;
    }
    let mut trailing = vec![true; glyphs.len()];
    let mut following = true;
    for i in (0..glyphs.len()).rev() {
        trailing[i] = following;
        if !is_space(&glyphs[i].text, properties) {
            following = removed.contains(&i);
        }
    }
    let mut previous = true;
    glyphs
        .into_iter()
        .enumerate()
        .filter_map(|(i, g)| {
            let space = is_space(&g.text, properties);
            let orphaned = space && previous && trailing[i];
            if !space {
                previous = removed.contains(&i);
            }
            (!removed.contains(&i) && !orphaned).then_some(g)
        })
        .collect()
}
/// The original Rust numerical kernel is reused to continuously complete repeated drawing candidates, translation evidence, connected components and source merging.
pub fn collapse_paints(
    mut glyphs: Vec<Glyph>,
    angles: &HashMap<u64, Angle>,
    properties: &HashMap<String, Properties>,
) -> Result<Vec<Glyph>, SnapshotError> {
    let mut signatures = HashMap::new();
    let records = glyphs
        .iter()
        .map(|g| {
            let h = &g.chars[0];
            let active = g.bbox.is_some()
                && !g.text.is_empty()
                && !is_space(&g.text, properties)
                && h.object.is_some()
                && h.origin.is_some()
                && h.mode.is_some_and(|v| (0..=6).contains(&v));
            if !active {
                return (None, None, 0, 0, false);
            }
            let key = (
                g.text.clone(),
                h.font,
                angles[&g.angle.to_bits()].rounded,
                h.mode,
                h.visible,
            );
            let next = signatures.len();
            let signature = *signatures.entry(key).or_insert(next);
            (g.bbox, h.origin, signature, h.object.unwrap(), true)
        })
        .collect();
    let (exact, offsets) =
        dedup::paint_pairs(records).ok_or(SnapshotError::Invalid("unsupported paint geometry"))?;
    let active: HashSet<usize> = offsets.iter().map(|p| p.0).collect();
    let mut texts = HashMap::new();
    let mut angle_ids = HashMap::new();
    let evidence = glyphs
        .iter()
        .enumerate()
        .map(|(index, g)| {
            let next = texts.len();
            let text = *texts.entry(g.text.as_str()).or_insert(next);
            let (projected, normal, angle_id) = if active.contains(&index) {
                let angle = angles[&g.angle.to_bits()];
                let origin = g.chars[0].origin.unwrap();
                let next = angle_ids.len();
                let id = *angle_ids.entry(angle.bucket).or_insert(next);
                (
                    Some(dedup::project(g.bbox.unwrap(), angle.cos, angle.sin)),
                    -angle.sin * origin[0] + angle.cos * origin[1],
                    id,
                )
            } else {
                (None, 0.0, 0)
            };
            (
                projected,
                normal,
                angle_id,
                text,
                is_space(&g.text, properties),
            )
        })
        .collect();
    let confirmed = dedup::confirmed_offsets(evidence, offsets, exact.clone());
    let roots = dedup::components(
        glyphs.len(),
        &exact.into_iter().chain(confirmed).collect::<Vec<_>>(),
    );
    for index in (0..glyphs.len()).rev() {
        if roots[index] != index {
            let duplicate = glyphs[index].clone();
            merge_sources(&mut glyphs[roots[index]], &duplicate);
        }
    }
    let removed = roots
        .iter()
        .enumerate()
        .filter_map(|(i, &root)| (root != i).then_some(i))
        .collect();
    Ok(retained(glyphs, &removed, properties))
}
/// Hidden text is paired with an ordered character index of visible text.
pub type HiddenPairs = Vec<(Vec<usize>, Vec<usize>)>;

/// To generate a hidden copy pair, the host then only needs to prepare the NFKC comparison metadata for the different entire strings.
pub fn hidden_pairs(
    glyphs: &[Glyph],
    angles: &HashMap<u64, Angle>,
    properties: &HashMap<String, Properties>,
) -> Result<HiddenPairs, SnapshotError> {
    if !glyphs.iter().any(|g| g.chars[0].mode == Some(3)) {
        return Ok(Vec::new());
    }
    let mut objects = HashMap::new();
    let records = glyphs
        .iter()
        .map(|g| {
            let head = &g.chars[0];
            let next = objects.len();
            let object = *objects.entry(head.object).or_insert(next);
            let angle = angles[&g.angle.to_bits()];
            let mode = head.mode.filter(|v| (0..=6).contains(v)).unwrap_or(-1) as i8;
            (
                g.bbox,
                head.origin,
                object,
                mode,
                head.visible.unwrap_or(true),
                !g.text.is_empty() && !is_space(&g.text, properties),
                g.angle,
                angle.cos,
                angle.sin,
                head.index,
            )
        })
        .collect();
    dedup::hidden_candidates(records)
        .ok_or(SnapshotError::Invalid("unsupported hidden-text geometry"))
}
/// Determine the entire pairing according to the pre-prepared host Unicode result, and retain the existing small number of Chinese character OCR fault tolerance thresholds.
pub fn suppress_hidden(
    mut glyphs: Vec<Glyph>,
    pairs: HiddenPairs,
    normalized: &HashMap<String, String>,
    properties: &HashMap<String, Properties>,
) -> Vec<Character> {
    let mut removed = HashSet::new();
    for (run, matches) in pairs {
        let a: String = run.iter().map(|&i| glyphs[i].text.as_str()).collect();
        let b: String = matches.iter().map(|&i| glyphs[i].text.as_str()).collect();
        let a = &normalized[&a];
        let b = &normalized[&b];
        let aa: Vec<char> = a.chars().collect();
        let bb: Vec<char> = b.chars().collect();
        if aa.is_empty() || aa.len() != bb.len() {
            continue;
        }
        if aa != bb {
            let count = aa.iter().zip(&bb).filter(|(a, b)| a == b).count();
            if count < 6
                || (count as f64) / (aa.len() as f64) < 0.85
                || !aa.iter().zip(&bb).all(|(a, b)| {
                    a == b
                        || (properties.get(&a.to_string()).is_some_and(|p| p.han)
                            && properties.get(&b.to_string()).is_some_and(|p| p.han))
                })
            {
                continue;
            }
        }
        removed.extend(run.iter().copied());
        if run.len() == matches.len()
            && run
                .iter()
                .zip(&matches)
                .all(|(&a, &b)| glyphs[a].chars.len() == glyphs[b].chars.len())
        {
            for (&a, &b) in run.iter().zip(&matches) {
                let duplicate = glyphs[a].clone();
                merge_sources(&mut glyphs[b], &duplicate);
            }
        } else {
            let values = sources(
                glyphs[matches[0]]
                    .chars
                    .iter()
                    .chain(run.iter().flat_map(|&i| glyphs[i].chars.iter())),
            );
            glyphs[matches[0]].chars[0].sources = values;
        }
    }
    retained(glyphs, &removed, properties)
        .into_iter()
        .flat_map(|g| g.chars)
        .collect()
}
