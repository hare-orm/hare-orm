//! The reader of a selected column whose field returns a value of its own type unchanged.

use pyo3::prelude::*;
use pyo3::types::PyType;
use pyo3::{PyTraverseError, PyVisit};

/// Reads a column of a field whose `from_db_value()` returns NULL and a value of exactly its type
/// unchanged: such a value is kept without a Python call, any other is handed to that `from_db_value()`.
#[pyclass(frozen, module = "rust.native.rows")]
pub struct TypedColumnReader {
    /// The field's type - a value of exactly it is kept.
    field_type: Py<PyType>,
    /// The field's `from_db_value` - for a value of another type.
    field_reader: Py<PyAny>,
}

impl TypedColumnReader {
    pub fn read<'py>(&self, raw: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = raw.py();
        if raw.is_none() || raw.get_type().is(self.field_type.bind(py)) {
            return Ok(raw);
        }
        self.field_reader.bind(py).call1((&raw,))
    }
}

#[pymethods]
impl TypedColumnReader {
    #[new]
    fn new(field_type: Py<PyType>, field_reader: Py<PyAny>) -> Self {
        TypedColumnReader { field_type, field_reader }
    }

    fn __call__<'py>(&self, raw: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.read(raw)
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.field_type)?;
        visit.call(&self.field_reader)
    }
}
