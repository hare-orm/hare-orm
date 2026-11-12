//! Prepared key and owner projections; shared instances belong to one execution only.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use pyo3::{PyTraverseError, PyVisit};

use crate::rows::column_reader::ColumnReader;
use crate::rows::model_row::{get_layout, ModelRowLayout};

#[pyclass(frozen, module = "rust.native.rows")]
pub struct GroupedModelLayout {
    layout: Py<ModelRowLayout>,
    positions: Vec<usize>,
    owner_key: Py<PyAny>,
    owner_reader: ColumnReader,
}

#[pymethods]
impl GroupedModelLayout {
    #[new]
    fn new(
        columns: &Bound<'_, PyAny>,
        key_fields: Vec<String>,
        owner_key: Py<PyAny>,
        owner_reader: Bound<'_, PyAny>,
    ) -> PyResult<Self> {
        let layout = get_layout(columns)?;
        let positions = key_fields
            .iter()
            .map(|name| layout.get().field_position(columns.py(), name))
            .collect::<PyResult<Vec<_>>>()?;
        Ok(Self { layout: layout.unbind(), positions, owner_key, owner_reader: ColumnReader::new(owner_reader) })
    }

    fn read_batch<'py>(
        &self,
        rows: &Bound<'py, PyAny>,
        start: usize,
        stop: usize,
        connection_alias: &Bound<'py, PyAny>,
        snapshot: &Bound<'py, PyAny>,
        shared: &Bound<'py, PyDict>,
    ) -> PyResult<Bound<'py, PyList>> {
        let py = rows.py();
        let layout = self.layout.get();
        let mut entries = Vec::with_capacity(stop.saturating_sub(start));
        for position in start..stop {
            let row = rows.get_item(position)?;
            let decoded = layout.decode(&row).collect::<PyResult<Vec<_>>>()?;
            let key = PyTuple::new(py, self.positions.iter().map(|position| &decoded[*position]))?;
            let owner = self.owner_reader.read(row.get_item(self.owner_key.bind(py))?)?;
            let instance = if let Some(instance) = shared.get_item(&key)? {
                instance
            } else {
                let instance = layout.construct(decoded.into_iter().map(Ok), connection_alias, snapshot)?;
                shared.set_item(key, &instance)?;
                instance
            };
            entries.push((owner, instance));
        }
        PyList::new(py, entries)
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.layout)?;
        visit.call(&self.owner_key)?;
        self.owner_reader.traverse(&visit)
    }
}
