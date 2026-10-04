//! Thin binding of font providers; life cycle and C callbacks are managed by the PDFium adaptation layer of the same library.
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
    /// Accept Python verified and kept-alive ABI addresses and fixed font resources.
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
    /// Release GIL when waiting for the reentrant state lock to avoid lock sequence deadlock caused by other callbacks waiting for GIL.
    fn install(&self, py: Python<'_>) -> PyResult<()> {
        py.detach(|| self.provider.install())
            .map_err(PyRuntimeError::new_err)
    }
    /// Convert permanent native callback failures into upper-layer classifiable exceptions.
    fn raise_if_failed(&self, py: Python<'_>) -> PyResult<()> {
        py.detach(|| self.provider.check())
            .map_err(PyRuntimeError::new_err)
    }
    /// Provide private ABI verification entry, return address cannot be used after provider recycling.
    fn _interface_address(&self) -> usize {
        self.provider.interface_address()
    }
    /// Return the release status, the number of fixed font requests and the number of complete font copies.
    fn stats(&self, py: Python<'_>) -> (bool, usize, usize) {
        py.detach(|| self.provider.stats())
    }
    /// Provides a name recognition differential entry from the reference implementation, without operating the PDFium global interface.
    fn classify(&self, py: Python<'_>, face: &[u8], charset: i32) -> Option<i32> {
        py.detach(|| self.provider.classify(face, charset))
    }
}
