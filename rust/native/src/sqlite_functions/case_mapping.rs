//! `Upper()`/`Lower()` and the case-insensitive lookups on SQLite, whose own `UPPER()`/`LOWER()` fold
//! ASCII only - ASCII text here, any other through the Python mapping (Postgres's per-character one).

use pyo3::prelude::*;
use pyo3::types::PyString;
use pyo3::{PyTraverseError, PyVisit};

/// The case mapping functions; text that isn't ASCII goes to the Python ones given.
#[pyclass(frozen, module = "rust.native.sqlite_functions")]
pub struct CaseMapping {
    upper_in_python: Py<PyAny>,
    lower_in_python: Py<PyAny>,
}

#[pymethods]
impl CaseMapping {
    #[new]
    fn new(upper_in_python: Py<PyAny>, lower_in_python: Py<PyAny>) -> Self {
        CaseMapping { upper_in_python, lower_in_python }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.upper_in_python)?;
        visit.call(&self.lower_in_python)
    }

    /// The upper-cased text; None stays None.
    fn upper<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        if let Ok(text) = value.cast::<PyString>() {
            let text = text.to_str()?;
            if text.is_ascii() {
                return Ok(PyString::new(value.py(), &text.to_ascii_uppercase()).into_any());
            }
        }
        self.upper_in_python.bind(value.py()).call1((value,))
    }

    /// The lower-cased text; None stays None.
    fn lower<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        if let Ok(text) = value.cast::<PyString>() {
            let text = text.to_str()?;
            if text.is_ascii() {
                return Ok(PyString::new(value.py(), &text.to_ascii_lowercase()).into_any());
            }
        }
        self.lower_in_python.bind(value.py()).call1((value,))
    }
}
