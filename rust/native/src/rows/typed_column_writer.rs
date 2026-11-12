//! Identity writes through the native serializer, with the field's one validation callback.

use pyo3::prelude::*;
use pyo3::types::PyType;
use pyo3::{PyTraverseError, PyVisit};

/// Values of the declared exact types need no conversion. Every other value uses the field's
/// ordinary writer. Validation errors propagate directly, without invoking a callback again.
#[pyclass(frozen, module = "rust.native.rows")]
pub struct TypedColumnWriter {
    exact_types: Vec<Py<PyType>>,
    null: bool,
    validate: Py<PyAny>,
    field_writer: Py<PyAny>,
}

impl TypedColumnWriter {
    pub fn write<'py>(&self, value: &Bound<'py, PyAny>, instance: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() && self.null {
            return Ok(value.clone());
        }
        let value_type = value.get_type();
        if self.exact_types.iter().any(|exact_type| value_type.is(exact_type.bind(py))) {
            self.validate.bind(py).call1((value,))?;
            return Ok(value.clone());
        }
        self.field_writer.bind(py).call1((value, instance))
    }
}

#[pymethods]
impl TypedColumnWriter {
    /// Args:
    ///     `exact_types`: The types whose values the field writes unchanged.
    ///     null: Whether a None value passes validation unchanged.
    ///     validate: The field's validation callback, called once for an unchanged non-null value.
    ///     `field_writer`: The ordinary writer for all other values, including its validation.
    #[new]
    fn new(exact_types: Vec<Py<PyType>>, null: bool, validate: Py<PyAny>, field_writer: Py<PyAny>) -> Self {
        TypedColumnWriter { exact_types, null, validate, field_writer }
    }

    fn __call__<'py>(&self, value: &Bound<'py, PyAny>, instance: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.write(value, instance)
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        for exact_type in &self.exact_types {
            visit.call(exact_type)?;
        }
        visit.call(&self.validate)?;
        visit.call(&self.field_writer)
    }
}
