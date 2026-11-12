//! Prepared model projections and relation attachments, independent of execution inputs.

use pyo3::prelude::*;
use pyo3::types::{PyList, PyString};
use pyo3::{PyTraverseError, PyVisit};

use crate::python::ffi::generic_set_attribute;
use crate::rows::model_row::{get_layout, ColumnEntry, ModelRowLayout};

struct RelatedLayout {
    layout: Py<ModelRowLayout>,
    parent: Option<usize>,
    attribute: Py<PyString>,
}

#[pyclass(frozen, module = "rust.native.rows")]
pub struct ModelResultLayout {
    base: Py<ModelRowLayout>,
    related: Vec<RelatedLayout>,
    annotations: Vec<ColumnEntry>,
}

#[pymethods]
impl ModelResultLayout {
    #[new]
    fn new(columns: &Bound<'_, PyAny>, annotations: &Bound<'_, PyAny>) -> PyResult<Self> {
        let py = columns.py();
        let layouts = columns.try_iter()?.map(|column| get_layout(&column?)).collect::<PyResult<Vec<_>>>()?;
        if layouts.is_empty() {
            return Err(pyo3::exceptions::PyValueError::new_err("model columns are empty"));
        }
        let related = layouts
            .iter()
            .enumerate()
            .skip(1)
            .map(|(index, layout)| {
                let layout = layout.get();
                let path = layout.path.bind(py);
                let Some(attribute) = layout.parent_attribute.as_ref() else {
                    return Err(pyo3::exceptions::PyValueError::new_err("related model path is empty"));
                };
                let parent_path = path.get_slice(0, path.len() - 1);
                let mut parent = None;
                for (candidate, previous) in layouts[..index].iter().enumerate() {
                    if previous.get().path.bind(py).eq(&parent_path)? {
                        parent = Some(candidate);
                        break;
                    }
                }
                Ok(RelatedLayout {
                    layout: layouts[index].clone().unbind(),
                    parent,
                    attribute: attribute.clone_ref(py),
                })
            })
            .collect::<PyResult<Vec<_>>>()?;
        Ok(Self {
            base: layouts[0].clone().unbind(),
            related,
            annotations: ModelRowLayout::prepare_annotations(annotations.extract()?),
        })
    }

    fn read_row<'py>(
        &self,
        row: &Bound<'py, PyAny>,
        connection_alias: &Bound<'py, PyAny>,
        snapshot: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.read(row, connection_alias, snapshot, &mut Vec::with_capacity(self.related.len() + 1))
    }

    fn read_batch<'py>(
        &self,
        rows: &Bound<'py, PyAny>,
        start: usize,
        stop: usize,
        connection_alias: &Bound<'py, PyAny>,
        snapshot: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyList>> {
        let mut instances = Vec::with_capacity(stop.saturating_sub(start));
        let mut hydrated = Vec::with_capacity(self.related.len() + 1);
        for position in start..stop {
            instances.push(self.read(&rows.get_item(position)?, connection_alias, snapshot, &mut hydrated)?);
        }
        PyList::new(rows.py(), instances)
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.base)?;
        for related in &self.related {
            visit.call(&related.layout)?;
            visit.call(&related.attribute)?;
        }
        for annotation in &self.annotations {
            annotation.traverse(&visit)?;
        }
        Ok(())
    }
}

impl ModelResultLayout {
    fn read<'py>(
        &self,
        row: &Bound<'py, PyAny>,
        connection_alias: &Bound<'py, PyAny>,
        snapshot: &Bound<'py, PyAny>,
        hydrated: &mut Vec<Option<Bound<'py, PyAny>>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = row.py();
        let instance = self.base.get().read(row, connection_alias, snapshot)?;
        hydrated.clear();
        hydrated.push(Some(instance.clone()));
        for related in &self.related {
            let child = related.layout.get().read(row, connection_alias, snapshot)?;
            if let Some(parent) = related.parent.and_then(|index| hydrated[index].as_ref()) {
                generic_set_attribute(parent, related.attribute.bind(py), &child)?;
            }
            hydrated.push((!child.is_none()).then_some(child));
        }
        ModelRowLayout::assign_annotations(&instance, row, &self.annotations)?;
        Ok(instance)
    }
}
