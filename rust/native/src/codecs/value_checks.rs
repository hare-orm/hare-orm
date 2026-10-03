//! A field's built-in validators as one call - `field.validate()` of a value of exactly the field's
//! type.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::inline_checks::InlineChecks;
use crate::codecs::options::Options;
use crate::python::references::PythonReferences;

#[pyclass(frozen, module = "rust.native.rows")]
pub struct ValueChecks {
    checks: InlineChecks,
}

#[pymethods]
impl ValueChecks {
    /// The checks of `checks`: `(kind, bound)` pairs in validator order, as a field codec's.
    #[new]
    fn new(field_name: &Bound<'_, PyString>, checks: &Bound<'_, PyAny>) -> PyResult<Self> {
        let values = PyDict::new(field_name.py());
        values.set_item("checks", checks)?;
        Ok(ValueChecks { checks: InlineChecks::from_options(&Options::new(field_name, values))? })
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        self.checks.traverse(&visit)
    }

    /// Raises the `ValidationError` the field's validators raise for `value`, named `field_name`.
    fn check(&self, value: &Bound<'_, PyAny>, field_name: &Bound<'_, PyString>) -> PyResult<()> {
        self.checks.check(value, field_name)
    }
}
