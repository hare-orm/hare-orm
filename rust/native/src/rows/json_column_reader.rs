//! The reader of a selected column of a built-in `JSONField`.

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyString};
use pyo3::{PyTraverseError, PyVisit};

use crate::rows::json_reading::loads_json;

/// Reads a column of a built-in `JSONField` - one with its own `from_db_value()`, the default decoder
/// and no declared value type - as that `from_db_value()` reads it: JSON text (`str` or `bytes`) is
/// decoded by `loads_json()`, any other value (NULL, a value the driver decoded) is kept. A text that
/// isn't JSON is handed to the field's `from_db_value()`, which raises the field's own error.
#[pyclass(frozen, module = "rust.native.rows")]
pub struct JsonColumnReader {
    /// The field's `from_db_value` - its error for a text that isn't JSON.
    field_reader: Py<PyAny>,
}

impl JsonColumnReader {
    pub fn read<'py>(&self, raw: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        if !raw.is_instance_of::<PyString>() && !raw.is_instance_of::<PyBytes>() {
            return Ok(raw);
        }
        match loads_json(&raw) {
            Ok(value) => Ok(value),
            Err(_) => self.field_reader.bind(raw.py()).call1((&raw,)),
        }
    }
}

#[pymethods]
impl JsonColumnReader {
    #[new]
    fn new(field_reader: Py<PyAny>) -> Self {
        JsonColumnReader { field_reader }
    }

    fn __call__<'py>(&self, raw: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.read(raw)
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.field_reader)
    }
}
