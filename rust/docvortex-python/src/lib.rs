//! 将 Python 批量参数转换为自有记录，再交给纯 Rust 核心计算。

use pyo3::prelude::*;

/// 一次提取已物化的数值特征，释放 GIL 后计算整段字符角色。
#[pyfunction]
fn script_roles(py: Python<'_>, records: Vec<docvortex_core::scripts::Record>) -> Option<Vec<u8>> {
    py.detach(move || docvortex_core::scripts::classify(records))
}

/// 注册私有扩展及协议号；公开 Python 接口仍由原模块提供。
#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("PROTOCOL_VERSION", docvortex_core::PROTOCOL_VERSION)?;
    module.add_function(wrap_pyfunction!(script_roles, module)?)?;
    Ok(())
}
