//! 本次表格构建的几何锚点与严格前缀状态；不保存最终表格认领结果。
use docvortex_core::columns::Columns;
use pyo3::prelude::*;
use pyo3::types::{PyFloat, PyList, PyTuple};
use std::collections::HashMap;

/// 页面构建期间保留来源行强引用，防止地址复用；生命周期与 Python 走廊缓存相同。
#[pyclass]
pub(super) struct NativeColumnCache {
    compensated: bool,
    prepared: HashMap<usize, (Py<PyAny>, Vec<(f64, f64)>)>,
    results: HashMap<(u64, Vec<usize>), (usize, f64)>,
    prefixes: HashMap<(u64, usize), (Vec<usize>, Columns)>,
}

#[pymethods]
impl NativeColumnCache {
    /// 当前解释器的求和模式由已核验的 Python 适配层传入。
    #[new]
    fn new(compensated: bool) -> Self {
        Self {
            compensated,
            prepared: HashMap::new(),
            results: HashMap::new(),
            prefixes: HashMap::new(),
        }
    }
    /// 横线起点变化后丢弃累计状态，保留当前构建内的几何锚点和纯几何结果。
    fn clear_prefixes(&mut self) {
        self.prefixes.clear();
    }

    /// 完整核验当前输入类型后读取未准备的锚点，严格保持来源顺序、首命中簇与平局优先级。
    fn count(
        &mut self,
        py: Python<'_>,
        rows: &Bound<'_, PyAny>,
        height: f64,
        allow_prefix: bool,
        row_type: &Bound<'_, PyAny>,
        fragment_type: &Bound<'_, PyAny>,
    ) -> PyResult<Option<(usize, f64)>> {
        if !height.is_finite() || !rows.is_exact_instance_of::<PyList>() {
            return Ok(None);
        }
        let mut ids = Vec::new();
        for row in rows.try_iter()? {
            let row = row?;
            if !row.get_type().is(row_type) {
                return Ok(None);
            }
            let id = row.as_ptr() as usize;
            if let std::collections::hash_map::Entry::Vacant(entry) = self.prepared.entry(id) {
                let fragments = row.getattr(pyo3::intern!(py, "fragments"))?;
                let Ok(fragments) = fragments.cast_exact::<PyList>() else {
                    return Ok(None);
                };
                let mut anchors = Vec::new();
                for fragment in fragments.iter() {
                    if !fragment.get_type().is(fragment_type) {
                        return Ok(None);
                    }
                    let bbox = fragment.getattr(pyo3::intern!(py, "local_bbox"))?;
                    if (!bbox.is_exact_instance_of::<PyList>()
                        && !bbox.is_exact_instance_of::<PyTuple>())
                        || bbox.len()? != 4
                    {
                        return Ok(None);
                    }
                    let mut values = [0.0; 2];
                    for (axis, value) in [0, 2].into_iter().zip(&mut values) {
                        let raw = bbox.get_item(axis)?;
                        if !raw.is_exact_instance_of::<PyFloat>() {
                            return Ok(None);
                        }
                        *value = raw.extract::<f64>()?;
                        if !value.is_finite() {
                            return Ok(None);
                        }
                    }
                    anchors.push((values[0], values[1]));
                }
                entry.insert((row.unbind(), anchors));
            }
            ids.push(id);
        }
        let bits = if height == 0.0 { 0 } else { height.to_bits() };
        let key = (bits, ids);
        if let Some(result) = self.results.get(&key) {
            return Ok(Some(*result));
        }
        let prefix_key = if allow_prefix {
            key.1.first().map(|first| (bits, *first))
        } else {
            None
        };
        let mut previous = prefix_key.and_then(|key| self.prefixes.remove(&key));
        let reuse = previous
            .as_ref()
            .is_some_and(|(before, _)| before.len() < key.1.len() && key.1.starts_with(before));
        let start = if reuse {
            previous.as_ref().unwrap().0.len()
        } else {
            0
        };
        let mut state = if reuse {
            previous.take().unwrap().1
        } else {
            Columns::new(self.compensated)
        };
        let rows: Vec<_> = key.1[start..]
            .iter()
            .map(|id| self.prepared[id].1.as_slice())
            .collect();
        let result = py.detach(|| state.extend_refs(&rows, 3.0_f64.max(height * 0.75)));
        if let Some(prefix_key) = prefix_key {
            if result.is_some() && (previous.is_none() || reuse) {
                self.prefixes.insert(prefix_key, (key.1.clone(), state));
            } else if result.is_some() {
                if let Some(previous) = previous {
                    self.prefixes.insert(prefix_key, previous);
                }
            }
        }
        if let Some(result) = result {
            self.results.insert(key, result);
        }
        Ok(result)
    }
}
