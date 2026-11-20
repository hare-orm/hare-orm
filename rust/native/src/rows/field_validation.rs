//! The default field's validator loop; validators remain live Python callables.

use pyo3::exceptions::PyException;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::PyType;

/// Preserve validator order and the field's error formatting, including sensitive values.
/// A custom Field.validate override does not enter this function unless it calls `super()`.
#[pyfunction]
pub fn validate_field(
    field: &Bound<'_, PyAny>,
    value: &Bound<'_, PyAny>,
    enum_type: &Bound<'_, PyType>,
) -> PyResult<()> {
    let py = value.py();
    if value.is_none() && field.getattr(intern!(py, "null"))?.is_truthy()? {
        return Ok(());
    }
    let checked = if value.is_instance(enum_type)? { value.getattr(intern!(py, "value"))? } else { value.clone() };
    for validator in field.getattr(intern!(py, "validators"))?.try_iter()? {
        if let Err(error) = validator?.call1((&checked,)) {
            if !error.is_instance_of::<PyException>(py) {
                return Err(error);
            }
            let validation_error =
                field.call_method1(intern!(py, "get_validation_error"), (error.into_value(py), value))?;
            if !validation_error.is_none() {
                return Err(PyErr::from_value(validation_error));
            }
        }
    }
    Ok(())
}
