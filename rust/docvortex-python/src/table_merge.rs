//! The Python object is constructed only at the boundary between the candidate semantic input and the final output, and the core members do not round trip candidate by candidate.
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
    /// Execute containment queries on a small number of annotation members without exporting the entire core collection.
    fn contains_all(&self, values: Vec<i64>) -> bool {
        values.iter().all(|v| self.members.contains(v))
    }
    /// Batch filter members outside the core to avoid each comment line number crossing the Python boundary individually.
    fn difference(&self, values: Vec<i64>) -> Vec<i64> {
        values
            .into_iter()
            .filter(|v| !self.members.contains(v))
            .collect()
    }
    /// Find the annotation intersection in batches, and only export the source line numbers that are really needed for the current boundary.
    fn intersection(&self, values: Vec<i64>) -> Vec<i64> {
        values
            .into_iter()
            .filter(|v| self.members.contains(v))
            .collect()
    }
    /// Export core members only by reference to adaptation or explicit enumeration.
    fn members(&self) -> Vec<i64> {
        self.members.iter().copied().collect()
    }
    /// The empty set can be judged without materialization, and the true and false value semantics of the Python annotation branch are maintained.
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
    /// Each page candidate group only prepares grid and row fragments once, and the life cycle does not depend on PDFium.
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
    /// To create a new one-time candidate stream, the caller must maintain the original stable score sorting.
    #[new]
    fn new() -> Self {
        Self {
            state: Some(Merger::default()),
        }
    }
    /// Grid expansion, shared member update and first target merging are performed continuously, and the status is changed after all inputs are verified.
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
    /// After consuming the candidate stream, only the result is materialized and retained; the repetition ends or continues to append a clear error.
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
