//! 将 Python 批量参数转换为自有记录，再交给纯 Rust 核心计算。

use pyo3::prelude::*;

/// 注册私有扩展及协议号；公开 Python 接口仍由原模块提供。
#[pymodule]
fn _native(module: &Bound<'_ , PyModule>) -> PyResult<()> {
    module.add("PROTOCOL_VERSION", docvortex_core::PROTOCOL_VERSION)?;
    Ok(())
}
