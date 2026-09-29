//! 文档风险累积器绑定：只在整行输入与最终结论边界接触 Python。
use docvortex_core::geometry_risk::{Entry, Risk, RunKey};
use pyo3::prelude::*;
use std::collections::HashMap;

#[pyclass(module = "docvortex._native")]
pub(super) struct NativeGeometryRuns {
    values: HashMap<RunKey, usize>,
}

#[pymethods]
impl NativeGeometryRuns {
    /// 创建全文共享的 run 编号表，保证跨页同 key 继续累计。
    #[new]
    fn new() -> Self {
        Self {
            values: HashMap::new(),
        }
    }

    /// 报告当前不同 run 数，仅用于测试和诊断。
    fn __len__(&self) -> usize {
        self.values.len()
    }
}

impl NativeGeometryRuns {
    /// 为完全相同的字体/角度/文字 key 分配稳定编号。
    pub(super) fn intern(&mut self, key: RunKey) -> usize {
        let next = self.values.len();
        *self.values.entry(key).or_insert(next)
    }
}

#[pyclass(module = "docvortex._native")]
pub(super) struct NativeGeometryRisk {
    state: Risk,
}

#[pymethods]
impl NativeGeometryRisk {
    /// 创建只拥有 Rust 数值统计的文档状态。
    #[new]
    fn new() -> Self {
        Self {
            state: Risk::default(),
        }
    }

    /// 释放 GIL 后消费一行；None 明确表示输入不属于有限标准数值域。
    fn add_line(
        &mut self,
        py: Python<'_>,
        page: usize,
        source: i64,
        height: f64,
        skip_y: bool,
        entries: Vec<Entry>,
    ) -> Option<bool> {
        py.detach(|| self.state.add_line(page, source, height, skip_y, entries))
    }

    /// 供页面级自有 evidence 在 Rust 内完成 run 编号后连续累积风险。
    pub(super) fn add_prepared(
        &mut self,
        py: Python<'_>,
        page: usize,
        source: i64,
        height: f64,
        skip_y: bool,
        entries: Vec<Entry>,
    ) -> Option<bool> {
        py.detach(|| self.state.add_line(page, source, height, skip_y, entries))
    }

    /// 返回布局与样式风险，不物化字符或字体字典。
    fn finish(&self, py: Python<'_>) -> (bool, bool) {
        py.detach(|| self.state.finish())
    }
}
