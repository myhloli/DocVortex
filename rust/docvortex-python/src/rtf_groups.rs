//! RTF 组范围的批量绑定：借用不可变 bytes，释放 GIL 后只运行纯字节扫描。

use pyo3::{prelude::*, types::PyBytes};
use std::sync::atomic::{AtomicUsize, Ordering};

type ScanRecord = (Vec<(usize, usize)>, Option<(bool, String)>);

static CALLS: AtomicUsize = AtomicUsize::new(0);
static BYTES: AtomicUsize = AtomicUsize::new(0);
static GROUPS: AtomicUsize = AtomicUsize::new(0);
static FAILURES: AtomicUsize = AtomicUsize::new(0);

/// 一次扫描返回组偏移或带分类的错误，由 Python 保留既有异常类型。
#[pyfunction]
pub(super) fn rtf_group_spans(
    py: Python<'_>,
    data: &Bound<'_, PyBytes>,
    direct_only: bool,
    max_tokens: usize,
    max_depth: usize,
) -> ScanRecord {
    let input = data.as_bytes();
    let result = py.detach(|| {
        docvortex_core::rtf_groups::group_spans(input, direct_only, max_tokens, max_depth)
    });
    CALLS.fetch_add(1, Ordering::Relaxed);
    BYTES.fetch_add(input.len(), Ordering::Relaxed);
    match result {
        Ok(groups) => {
            GROUPS.fetch_add(groups.len(), Ordering::Relaxed);
            (groups, None)
        }
        Err(error) => {
            FAILURES.fetch_add(1, Ordering::Relaxed);
            (Vec::new(), Some((error.resource_limit, error.message)))
        }
    }
}

/// 返回已完成扫描的调用、输入字节、组数和失败次数，供真实入口核验。
#[pyfunction]
pub(super) fn rtf_group_stats() -> (usize, usize, usize, usize) {
    (
        CALLS.load(Ordering::Relaxed),
        BYTES.load(Ordering::Relaxed),
        GROUPS.load(Ordering::Relaxed),
        FAILURES.load(Ordering::Relaxed),
    )
}
