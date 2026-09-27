//! 自有文档入口：坐标与统计在 Rust 连续执行，按后续规则需要决定是否物化字符。
use super::geometry::{plain_coordinates, shared_coordinates};
use super::geometry_runs::materialize_runs;
use docvortex_core::{
    geometry::{self, Box4, Size},
    geometry_lines::{self, World},
    geometry_runs::Sample,
    geometry_style::{Document, Result},
};
use pyo3::{
    exceptions::PyValueError,
    prelude::*,
    types::{PyBool, PyDict, PyFloat, PyInt, PyList, PySet, PyTuple},
};

struct Frame {
    source: Box4,
    legacy: Option<Box4>,
    size: Size,
    tight: Box4,
    origin: Size,
    angle: i32,
    line: usize,
    owners: [Py<PyAny>; 4],
    char_index: Py<PyAny>,
    text: Py<PyAny>,
    font_size: Py<PyAny>,
}

#[pyclass(module = "docvortex._native")]
pub(super) struct NativeStyleDocument {
    document: Option<Document>,
    with_samples: bool,
    frames: Vec<Frame>,
    lines: Vec<Py<PyAny>>,
}

#[pymethods]
impl NativeStyleDocument {
    /// 新建单次消费的自有文档；仅布局分支保留最终物化所需的 Python 来源对象。
    #[new]
    #[pyo3(signature = (with_samples=false))]
    fn new(with_samples: bool) -> Self {
        Self {
            document: Some(Document::default()),
            with_samples,
            frames: Vec::new(),
            lines: Vec::new(),
        }
    }

    /// 融合原始框选择、裁剪与旋转，仅把有效位置索引送给字体编码回调。
    #[pyo3(signature = (records, size, angle, page, source, height, metadata, line=None))]
    fn add_line(
        &mut self,
        py: Python<'_>,
        records: &Bound<'_, PyList>,
        size: Size,
        angle: i32,
        page: i64,
        source: i64,
        height: f64,
        metadata: &Bound<'_, PyAny>,
        line: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<bool> {
        let document = self
            .document
            .as_mut()
            .ok_or_else(|| PyValueError::new_err("geometry document already consumed"))?;
        if !height.is_finite() || size.iter().any(|v| !v.is_finite()) {
            return Ok(false);
        }
        if self.with_samples && line.is_none() {
            return Err(PyValueError::new_err("layout document requires line owner"));
        }
        let mut rows = Vec::with_capacity(records.len());
        let mut positions = Vec::with_capacity(records.len());
        let mut owners = Vec::new();
        let mut raw_sources = Vec::new();
        for record in records.iter() {
            let Ok(record) = record.cast_exact::<PyTuple>() else {
                return Ok(false);
            };
            if record.len() != 6 {
                return Ok(false);
            }
            let position = record.get_item(0)?;
            if !position.is_exact_instance_of::<PyInt>() {
                return Ok(false);
            }
            let Ok(position) = position.extract::<i64>() else {
                return Ok(false);
            };
            if position < 0 {
                return Ok(false);
            }
            let raw = record.get_item(1)?;
            let side = record.get_item(2)?;
            let tight = record.get_item(3)?;
            let origin = record.get_item(4)?;
            let (Some(r), Some(s), Some(t), Some(o)) = (
                plain_coordinates::<4>(&raw),
                plain_coordinates::<4>(&side),
                plain_coordinates::<4>(&tight),
                plain_coordinates::<2>(&origin),
            ) else {
                return Ok(false);
            };
            let rotation = record.get_item(5)?;
            if !rotation.is_exact_instance_of::<PyFloat>() {
                return Ok(false);
            }
            let rotation = rotation.extract::<f64>()?;
            if !rotation.is_finite() {
                return Ok(false);
            }
            if self.with_samples {
                raw_sources.push(r);
                owners.push(Some([
                    raw.unbind(),
                    side.unbind(),
                    tight.unbind(),
                    origin.unbind(),
                ]));
            }
            positions.push(position);
            rows.push((r, s, t, o, rotation));
        }
        let prepared = py.detach(move || geometry::source_rows(rows, size, angle));
        let rows: Vec<_> = positions
            .into_iter()
            .zip(prepared)
            .enumerate()
            .filter_map(|(index, (position, row))| row.map(|r| (index, position, r)))
            .collect();
        if rows.is_empty() {
            return Ok(true);
        }
        let positions: Vec<_> = rows.iter().map(|r| r.1).collect();
        let data = metadata.call1((positions,))?;
        // 字体回调在执行自定义转换前可明确拒绝；其他异常不拦截。
        if data.is_none() {
            return Ok(false);
        }
        let (encoded, mut labels) = if self.with_samples {
            let values: Vec<(usize, f64, bool, Py<PyAny>, Py<PyAny>)> = data.extract()?;
            let mut encoded = Vec::with_capacity(values.len());
            let mut labels = Vec::with_capacity(values.len());
            for (index, (run, size, anchor, char_index, text)) in values.into_iter().enumerate() {
                let raw_size = data.get_item(index)?.get_item(1)?;
                let raw_size = if raw_size.is_exact_instance_of::<PyFloat>() {
                    raw_size.unbind()
                } else {
                    PyFloat::new(py, size).into_any().unbind()
                };
                encoded.push((run, size, anchor));
                labels.push((char_index, text, raw_size));
            }
            (encoded, labels)
        } else {
            (data.extract::<Vec<(usize, f64, bool)>>()?, Vec::new())
        };
        if encoded.len() != rows.len() {
            return Err(PyValueError::new_err("geometry metadata count differs"));
        }
        if encoded.iter().any(|v| !v.1.is_finite()) {
            return Ok(false);
        }
        let line_index = self.lines.len();
        if let Some(line) = line {
            if self.with_samples {
                self.lines.push(line.clone().unbind());
            }
        }
        let mut records = Vec::with_capacity(rows.len());
        let mut labels = labels.drain(..);
        for ((index, position, row), (run, font_size, anchor)) in rows.into_iter().zip(encoded) {
            if self.with_samples {
                let (char_index, text, raw_size) = labels.next().unwrap();
                self.frames.push(Frame {
                    source: row.0,
                    legacy: raw_sources[index],
                    size,
                    tight: row.1,
                    origin: row.2,
                    angle,
                    line: line_index,
                    owners: owners[index].take().unwrap(),
                    char_index,
                    text,
                    font_size: raw_size,
                });
            }
            records.push(Sample {
                run,
                position,
                source: row.3,
                tight: row.4,
                origin: row.5,
                size: font_size,
                anchor,
            });
        }
        document.append(page, source, height, records);
        Ok(true)
    }

    /// 样式分支只导出最终摘要；重复结束或继续追加明确报错。
    fn finish(&mut self, py: Python<'_>, families: Vec<usize>) -> PyResult<Option<Result>> {
        let document = self
            .document
            .take()
            .ok_or_else(|| PyValueError::new_err("geometry document already consumed"))?;
        self.frames.clear();
        self.lines.clear();
        Ok(py.detach(move || document.finish(families)))
    }

    /// 布局分支连续完成样式及来源恢复，再仅物化一次最终样本和 run。
    #[pyo3(signature = (families, keys, sample_type, run_type, on_ready=None, with_metrics=false, compensated=false))]
    fn finish_layout<'py>(
        &mut self,
        py: Python<'py>,
        families: Vec<usize>,
        keys: &Bound<'py, PyList>,
        sample_type: &Bound<'py, PyAny>,
        run_type: &Bound<'py, PyAny>,
        on_ready: Option<&Bound<'py, PyAny>>,
        with_metrics: bool,
        compensated: bool,
    ) -> PyResult<Option<Bound<'py, PyAny>>> {
        let document = self
            .document
            .take()
            .ok_or_else(|| PyValueError::new_err("geometry document already consumed"))?;
        let mut frames = std::mem::take(&mut self.frames);
        let lines = std::mem::take(&mut self.lines);
        let legacy = frames.iter().map(|f| (f.legacy, f.size, f.angle)).collect();
        let world = if with_metrics {
            let mut flags = Vec::with_capacity(lines.len());
            for line in &lines {
                let line = line.bind(py);
                let canonical = !line.getattr("formula_candidate_only")?.extract::<bool>()?
                    && !line.getattr("compact_formula_cluster")?.extract::<bool>()?;
                let y_eligible = canonical
                    && line.getattr("angle")?.extract::<i32>()? == 0
                    && !line.getattr("restored_inline_cluster")?.extract::<bool>()?;
                flags.push((canonical, y_eligible));
            }
            frames
                .iter()
                .map(|f| World {
                    tight: f.tight,
                    canonical: flags[f.line].0,
                    y_eligible: flags[f.line].1,
                })
                .collect()
        } else {
            Vec::new()
        };
        let result = py.detach(move || {
            let prepared = document.prepare(&families, Some(legacy))?;
            let summary = if with_metrics {
                Some((
                    geometry_lines::summarize(&prepared, &world, compensated)?,
                    docvortex_core::geometry_style::reports(&prepared.runs, &prepared.style),
                ))
            } else {
                None
            };
            Some((prepared, summary))
        });
        let Some((prepared, summary)) = result else {
            return Ok(None);
        };
        if frames.len() != prepared.samples.len() {
            return Ok(None);
        }
        let needs_samples = summary.as_ref().is_none_or(|(metrics, _)| {
            metrics.2 || prepared.runs.iter().any(|r| r.strong || r.sibling)
        });
        for (frame, restored) in frames.iter_mut().zip(&prepared.restored) {
            if let Some(source) = restored {
                frame.source = *source;
            }
        }
        let inflated = PySet::empty(py)?;
        let mut flags = vec![false; prepared.runs.len()];
        for index in &prepared.style.inflated {
            inflated.add(keys.get_item(*index)?)?;
            flags[*index] = true;
        }
        let scales = PyDict::new(py);
        for (index, scale) in &prepared.style.scales {
            scales.set_item(prepared.line_keys[*index], *scale)?;
        }
        // 接受原生输入后先释放无用侧表，再创建布局对象，限制瞬时存活量和 GC 扫描。
        if let Some(on_ready) = on_ready {
            on_ready.call0()?;
        }
        let samples = PyList::empty(py);
        if needs_samples {
            for ((sample, metadata), frame) in
                prepared.samples.iter().zip(&prepared.metadata).zip(frames)
            {
                let [raw, side, tight, origin] = frame.owners;
                let source = shared_coordinates(
                    py,
                    frame.source,
                    &[raw.into_bound(py), side.into_bound(py)],
                )?;
                let tight = shared_coordinates(py, frame.tight, &[tight.into_bound(py)])?;
                let origin = shared_coordinates(py, frame.origin, &[origin.into_bound(py)])?;
                let (local_source, local_tight, local_origin) = if frame.angle == 0 {
                    (source.clone(), tight.clone(), origin.clone())
                } else {
                    (
                        shared_coordinates(py, sample.source, &[])?,
                        shared_coordinates(py, sample.tight, &[])?,
                        shared_coordinates(py, sample.origin, &[])?,
                    )
                };
                let fields = PyTuple::new(
                    py,
                    [
                        metadata.page.into_pyobject(py)?.into_any(),
                        lines[frame.line].bind(py).clone(),
                        sample.position.into_pyobject(py)?.into_any(),
                        frame.char_index.into_bound(py),
                        frame.text.into_bound(py),
                        source,
                        tight,
                        origin,
                        local_source,
                        local_tight,
                        local_origin,
                        keys.get_item(sample.run)?,
                        frame.font_size.into_bound(py),
                        PyBool::new(py, sample.anchor).to_owned().into_any(),
                    ],
                )?;
                samples.append(sample_type.call1(fields)?)?;
            }
        }
        let by_line = PyDict::new(py);
        if needs_samples {
            for (key, indices) in prepared.line_keys.into_iter().zip(prepared.lines) {
                let members = PyList::empty(py);
                for index in indices {
                    members.append(samples.get_item(index)?)?;
                }
                by_line.set_item(key, members)?;
            }
        }
        let runs = if needs_samples {
            materialize_runs(py, &samples, keys, prepared.runs, &flags, run_type)?
        } else {
            PyDict::new(py)
        };
        if let Some(summary) = summary {
            Ok(Some(
                (samples, by_line, runs, inflated, scales, summary)
                    .into_pyobject(py)?
                    .into_any(),
            ))
        } else {
            Ok(Some(
                (samples, by_line, runs, inflated, scales)
                    .into_pyobject(py)?
                    .into_any(),
            ))
        }
    }
}
