//! 字体提供器的薄绑定；生命周期与 C 回调由同库 PDFium 适配层管理。
use docvortex_pdfium::fonts::FontProvider;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use std::collections::HashMap;
use std::sync::Arc;

#[pyclass(module = "docvortex._native")]
pub struct NativeFontProvider {
    provider: Arc<FontProvider>,
}

#[pymethods]
impl NativeFontProvider {
    /// 接受 Python 已验证并保活的 ABI 地址和固定字体资源。
    #[new]
    fn new(
        addresses: [usize; 3],
        data: Vec<u8>,
        tables: HashMap<u32, (usize, usize)>,
        cache_name: Vec<u8>,
        aliases: HashMap<String, i32>,
        suffixes: Vec<String>,
        classify_legacy: usize,
    ) -> PyResult<Self> {
        let provider = unsafe {
            FontProvider::new(
                addresses,
                data,
                tables,
                cache_name,
                aliases,
                suffixes,
                classify_legacy,
            )
        }
        .map_err(PyRuntimeError::new_err)?;
        Ok(Self { provider })
    }
    /// 等待可重入状态锁时释放 GIL，避免其它回调等待 GIL 造成锁顺序死锁。
    fn install(&self, py: Python<'_>) -> PyResult<()> {
        py.detach(|| self.provider.install())
            .map_err(PyRuntimeError::new_err)
    }
    /// 将永久原生回调故障转换为上层可分类异常。
    fn raise_if_failed(&self, py: Python<'_>) -> PyResult<()> {
        py.detach(|| self.provider.check())
            .map_err(PyRuntimeError::new_err)
    }
    /// 提供私有 ABI 验证入口，不能在提供器回收后使用返回地址。
    fn _interface_address(&self) -> usize {
        self.provider.interface_address()
    }
    /// 返回释放状态、固定字体请求数及完整字库复制数。
    fn stats(&self, py: Python<'_>) -> (bool, usize, usize) {
        py.detach(|| self.provider.stats())
    }
    /// 提供与参考实现的名称识别差分入口，不操作 PDFium 全局接口。
    fn classify(&self, py: Python<'_>, face: &[u8], charset: i32) -> Option<i32> {
        py.detach(|| self.provider.classify(face, charset))
    }
}
