//! Identity writes through the native serializer and the field's single validation implementation.

use pyo3::prelude::*;
use pyo3::types::PyType;
use pyo3::{PyTraverseError, PyVisit};

use crate::rows::field_validation::validate_field;

enum ColumnValidation {
    Native { field: Py<PyAny>, enum_type: Py<PyType> },
    Python(Py<PyAny>),
}

/// Values of the declared exact types need no conversion. Every other value uses the field's
/// ordinary writer. Validation errors propagate directly, without invoking a callback again.
#[pyclass(frozen, module = "rust.native.rows")]
pub struct TypedColumnWriter {
    exact_types: Vec<Py<PyType>>,
    null: bool,
    validation: ColumnValidation,
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
            match &self.validation {
                ColumnValidation::Native { field, enum_type } => {
                    validate_field(field.bind(py), value, enum_type.bind(py))?;
                }
                ColumnValidation::Python(validate) => {
                    validate.bind(py).call1((value,))?;
                }
            }
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
    ///     validation: `(field, Enum)` for the default implementation, `(callback, None)` for a captured callback.
    ///     `field_writer`: The ordinary writer for all other values, including its validation.
    #[new]
    fn new(
        exact_types: Vec<Py<PyType>>,
        null: bool,
        validation: (Py<PyAny>, Option<Py<PyType>>),
        field_writer: Py<PyAny>,
    ) -> Self {
        let (payload, enum_type) = validation;
        let validation = match enum_type {
            Some(enum_type) => ColumnValidation::Native { field: payload, enum_type },
            None => ColumnValidation::Python(payload),
        };
        TypedColumnWriter { exact_types, null, validation, field_writer }
    }

    fn __call__<'py>(&self, value: &Bound<'py, PyAny>, instance: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.write(value, instance)
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        for exact_type in &self.exact_types {
            visit.call(exact_type)?;
        }
        match &self.validation {
            ColumnValidation::Native { field, enum_type } => {
                visit.call(field)?;
                visit.call(enum_type)?;
            }
            ColumnValidation::Python(validate) => visit.call(validate)?,
        }
        visit.call(&self.field_writer)
    }
}
