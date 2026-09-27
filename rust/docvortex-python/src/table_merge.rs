//! 仅在候选语义输入与最终输出边界构造 Python 对象，核心成员不逐候选往返。
use docvortex_core::geometry::{Box4, Size};
use docvortex_core::table_merge::{
    self, Annotation, Candidate, GridContext, Ids, Members, Merger, Row,
};
use pyo3::{exceptions::PyValueError, prelude::*};
use std::sync::Arc;

#[pyclass(frozen, module = "docvortex._native")]
pub(super) struct OwnedRuleCore {
    pub(super) members: Arc<Ids>,
}
#[pymethods]
impl OwnedRuleCore {
    /// 对少量注释成员执行包含查询，不导出整个核心集合。
    fn contains_all(&self, values: Vec<i64>) -> bool {
        values.iter().all(|v| self.members.contains(v))
    }
    /// 批量筛选核心之外的成员，避免每个注释行号单独跨越 Python 边界。
    fn difference(&self, values: Vec<i64>) -> Vec<i64> {
        values
            .into_iter()
            .filter(|v| !self.members.contains(v))
            .collect()
    }
    /// 批量求注释交集，只导出当前边界确实需要的来源行号。
    fn intersection(&self, values: Vec<i64>) -> Vec<i64> {
        values
            .into_iter()
            .filter(|v| self.members.contains(v))
            .collect()
    }
    /// 仅参考适配或显式枚举时导出核心成员。
    fn members(&self) -> Vec<i64> {
        self.members.iter().copied().collect()
    }
    /// 不物化即可判断空集合，保持 Python 注释分支的真假值语义。
    fn __len__(&self) -> usize {
        self.members.len()
    }
}

#[pyclass(module = "docvortex._native")]
pub(super) struct NativeTableGrid {
    state: GridContext,
}
#[pymethods]
impl NativeTableGrid {
    /// 每个页面候选组只准备一次网格和行片段，生命周期不依赖 PDFium。
    #[new]
    fn new(
        py: Python<'_>,
        grids: Vec<Box4>,
        rows: Vec<Row>,
        size: Size,
        angle: i32,
        height: f64,
    ) -> PyResult<Self> {
        py.detach(|| GridContext::new(grids, rows, size, angle, height))
            .map(|state| Self { state })
            .ok_or_else(|| PyValueError::new_err("invalid table grid data"))
    }
}

type AnnotationInput = (String, Box4, Vec<i64>, Vec<(i64, Box4)>);
type Output = (
    Box4,
    Box4,
    i32,
    f64,
    Option<Box4>,
    Vec<i64>,
    Vec<AnnotationInput>,
);
#[pyclass(module = "docvortex._native")]
pub(super) struct NativeTableMerger {
    state: Option<Merger>,
}
#[pymethods]
impl NativeTableMerger {
    /// 新建一次性候选流，调用方须保持原有稳定分数排序。
    #[new]
    fn new() -> Self {
        Self {
            state: Some(Merger::default()),
        }
    }
    /// 连续执行网格扩展、共享成员更新和首目标合并，全部输入验证后才改变状态。
    #[pyo3(signature=(bbox, local, angle, score, core, members, annotations, owned=None, grid=None))]
    fn add(
        &mut self,
        py: Python<'_>,
        bbox: Box4,
        local: Box4,
        angle: i32,
        score: f64,
        core: Option<Box4>,
        members: Vec<i64>,
        annotations: Vec<AnnotationInput>,
        owned: Option<PyRef<'_, OwnedRuleCore>>,
        mut grid: Option<PyRefMut<'_, NativeTableGrid>>,
    ) -> PyResult<()> {
        let state = self
            .state
            .as_mut()
            .ok_or_else(|| PyValueError::new_err("table merger already consumed"))?;
        if !table_merge::valid(&bbox)
            || !table_merge::valid(&local)
            || core.is_some_and(|b| !table_merge::valid(&b))
            || !score.is_finite()
            || annotations.iter().any(|a| {
                !table_merge::valid(&a.1) || a.3.iter().any(|(_, b)| !table_merge::valid(b))
            })
        {
            return Err(PyValueError::new_err("invalid table candidate data"));
        }
        let annotations: Vec<_> = annotations
            .into_iter()
            .map(|(kind, bbox, members, rows)| Annotation {
                kind,
                bbox,
                members: members.into_iter().collect(),
                rows,
            })
            .collect();
        let excluded = annotations
            .iter()
            .flat_map(|a| a.members.iter().copied())
            .collect();
        let members = if let Some(owned) = owned {
            Members::new(owned.members.clone(), excluded)
        } else {
            Members::plain(members.into_iter().collect())
        };
        let mut candidate = Candidate {
            bbox,
            local,
            angle,
            score,
            core,
            members,
            annotations,
        };
        let grid = grid.as_mut().map(|g| &mut g.state);
        py.detach(move || {
            if let Some(grid) = grid {
                grid.expand(&mut candidate);
            }
            state.push(candidate);
        });
        Ok(())
    }
    /// 消费候选流后仅物化保留结果；重复结束或继续追加明确报错。
    fn finish(&mut self, py: Python<'_>) -> PyResult<Vec<Output>> {
        let state = self
            .state
            .take()
            .ok_or_else(|| PyValueError::new_err("table merger already consumed"))?;
        Ok(py.detach(move || {
            state
                .finish()
                .into_iter()
                .map(|c| {
                    let members = c.members.values().collect();
                    let annotations = c
                        .annotations
                        .into_iter()
                        .map(|a| (a.kind, a.bbox, a.members.into_iter().collect(), a.rows))
                        .collect();
                    (
                        c.bbox,
                        c.local,
                        c.angle,
                        c.score,
                        c.core,
                        members,
                        annotations,
                    )
                })
                .collect()
        }))
    }
}
