//! 原始分类统计的独立快照，字体规范化只跨边界执行一次/字体。
use docvortex_core::text_classification::{self, Counts};
use docvortex_pdfium::ReadError;
use pyo3::{
    exceptions::{PyMemoryError, PyValueError},
    prelude::*,
    types::{PyBytes, PyDict},
};
use std::{
    collections::HashSet,
    sync::atomic::{AtomicU64, Ordering},
};

static CALLS: AtomicU64 = AtomicU64::new(0);
static IMAGE_CALLS: AtomicU64 = AtomicU64::new(0);
static PIXEL_CALLS: AtomicU64 = AtomicU64::new(0);

#[pyclass(frozen, module = "docvortex._native")]
pub struct NativeClassificationSnapshot {
    counts: Counts,
    names: Vec<String>,
}

#[pymethods]
impl NativeClassificationSnapshot {
    /// 输出独立可变字典，保持原始字体出现顺序，并合并规范化后相同的字体名称。
    fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let output = PyDict::new(py);
        for (name, value) in [
            ("null_char_count", self.counts.null),
            ("replacement_char_count", self.counts.replacement),
            ("control_char_count", self.counts.control),
            ("private_use_char_count", self.counts.private),
            ("unicode_map_error_count", self.counts.map_error),
        ] {
            output.set_item(name, value)?;
        }
        for (field, position) in [
            ("font_name_counts", 0),
            ("font_non_generated_char_counts", 1),
            ("font_non_generated_cjk_char_counts", 2),
        ] {
            let values = PyDict::new(py);
            // 各统计字典按各自首次满足条件的字符排序，不能共用字体首次出现顺序。
            let mut indices: Vec<_> = (0..self.names.len()).collect();
            indices.sort_by_key(|&index| self.counts.font_first[index][position]);
            for index in indices {
                let name = &self.names[index];
                let (all, non_generated, cjk) = self.counts.fonts[index];
                let count = [all, non_generated, cjk][position];
                if name.is_empty() || count == 0 {
                    continue;
                }
                let previous = values
                    .get_item(name)?
                    .map(|v| v.extract::<usize>())
                    .transpose()?
                    .unwrap_or(0);
                values.set_item(name, previous + count)?;
            }
            output.set_item(field, values)?;
            if position == 0 {
                output.set_item("non_generated_char_count", self.counts.non_generated)?;
            }
        }
        Ok(output)
    }
}

/// 借用同库文本页读取全部原始记录后释放 PDFium 依赖，再进行纯 Rust 汇总。
#[pyfunction]
pub fn read_pdfium_classification(
    py: Python<'_>,
    addresses: [usize; 4],
    handle: usize,
    count: usize,
    cjk_ranges: Vec<(u32, u32)>,
    allowed_controls: Vec<u32>,
    private_range: (u32, u32),
    normalize_font: &Bound<'_, PyAny>,
) -> PyResult<NativeClassificationSnapshot> {
    let (records, fonts) =
        unsafe { docvortex_pdfium::classification::read(addresses, handle, count) }.map_err(
            |error| match error {
                ReadError::InvalidInput(message) => PyValueError::new_err(message),
                ReadError::Pdfium(message) => super::pdfium::PdfiumReadError::new_err(message),
                ReadError::Allocation(message) => PyMemoryError::new_err(message),
            },
        )?;
    let names = fonts
        .iter()
        .map(|name| {
            let decoded = PyBytes::new(py, name).call_method1("decode", ("utf-8", "ignore"))?;
            normalize_font.call1((decoded,))?.extract::<String>()
        })
        .collect::<PyResult<Vec<_>>>()?;
    let allowed: HashSet<_> = allowed_controls.into_iter().collect();
    let counts = py.detach(|| {
        text_classification::count(&records, fonts.len(), &cjk_ranges, &allowed, private_range)
    });
    CALLS.fetch_add(1, Ordering::Relaxed);
    Ok(NativeClassificationSnapshot { counts, names })
}

/// 统计真实原始分类读取次数，与 canonical 字符提取计数分开报告。
#[pyfunction]
pub fn classification_snapshot_stats() -> u64 {
    CALLS.load(Ordering::Relaxed)
}

#[pyclass(frozen, module = "docvortex._native")]
pub struct NativeImageTextSnapshot {
    candidates: docvortex_core::classification_visuals::Candidates,
}

#[pymethods]
impl NativeImageTextSnapshot {
    /// 返回候选字符数，不逐字符物化 Python 字典或元组。
    #[getter]
    fn count(&self) -> usize {
        self.candidates.boxes.len()
    }

    /// 只描述对象绘制可能性，实际是否被图片遮挡仍由原像素核验裁决。
    #[getter]
    fn paints(&self) -> bool {
        self.candidates.paints
    }

    /// 仅供诊断和字段级差分测试，返回脱离快照的候选框副本。
    fn boxes(&self) -> Vec<[f64; 4]> {
        self.candidates.boxes.clone()
    }

    /// 批量消费独立的 L 模式差分字节，页面关闭后仍可查询，不持有原生句柄。
    fn painted_count(
        &self,
        py: Python<'_>,
        data: &Bound<'_, PyBytes>,
        width: usize,
        height: usize,
        page_width: f64,
        page_height: f64,
    ) -> PyResult<usize> {
        if width.checked_mul(height) != Some(data.as_bytes().len())
            || !page_width.is_finite()
            || !page_height.is_finite()
            || page_width <= 0.0
            || page_height <= 0.0
        {
            return Err(PyValueError::new_err(
                "invalid image text pixel buffer or page size",
            ));
        }
        let bytes = data.as_bytes();
        let count = py.detach(|| {
            docvortex_core::classification_visuals::painted_count(
                bytes,
                width,
                height,
                &self.candidates.boxes,
                page_width,
                page_height,
            )
        });
        PIXEL_CALLS.fetch_add(1, Ordering::Relaxed);
        Ok(count)
    }
}

/// 标准 ABI 下同步读取原始字符后释放 PDFium 依赖，纯几何部分可释放 GIL。
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn read_pdfium_image_text(
    py: Python<'_>,
    addresses: [usize; 3],
    handle: usize,
    count: usize,
    frame: [f64; 4],
    rotation: i32,
    images: Vec<[f64; 4]>,
    visibility: Vec<docvortex_core::classification_visuals::Visibility>,
    overlap: f64,
) -> PyResult<Option<NativeImageTextSnapshot>> {
    let records =
        unsafe { docvortex_pdfium::classification::read_image_text(addresses, handle, count) }
            .map_err(|error| match error {
                ReadError::InvalidInput(message) => PyValueError::new_err(message),
                ReadError::Pdfium(message) => super::pdfium::PdfiumReadError::new_err(message),
                ReadError::Allocation(message) => PyMemoryError::new_err(message),
            })?;
    let candidates = py.detach(|| {
        docvortex_core::classification_visuals::candidates(
            &records,
            frame,
            rotation,
            &images,
            &visibility,
            overlap,
        )
    });
    IMAGE_CALLS.fetch_add(1, Ordering::Relaxed);
    Ok(candidates.map(|candidates| NativeImageTextSnapshot { candidates }))
}

/// 分别报告成功完成的批量读取与像素查询次数，不与原始字体统计混计。
#[pyfunction]
pub fn image_text_snapshot_stats() -> (u64, u64) {
    (
        IMAGE_CALLS.load(Ordering::Relaxed),
        PIXEL_CALLS.load(Ordering::Relaxed),
    )
}
