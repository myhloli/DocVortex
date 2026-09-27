//! 文档风险累积器绑定：只在整行输入与最终结论边界接触 Python。
use docvortex_core::geometry_risk::{Entry, Risk};
use pyo3::prelude::*;

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

    /// 返回布局与样式风险，不物化字符或字体字典。
    fn finish(&self, py: Python<'_>) -> (bool, bool) {
        py.detach(|| self.state.finish())
    }
}
