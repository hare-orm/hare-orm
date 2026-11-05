//! Python classes and objects the codecs use on every value, imported once per interpreter.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyType};

static UUID_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static DECIMAL_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static UTC_TIMEZONE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// `uuid.UUID`.
pub fn uuid_type(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    UUID_TYPE.import(py, "uuid", "UUID")
}

/// Construct a UUID through its public initializer, including its standard safety metadata.
pub fn new_uuid(py: Python<'_>, value: u128) -> PyResult<Bound<'_, PyAny>> {
    let arguments = PyDict::new(py);
    arguments.set_item(intern!(py, "int"), value)?;
    uuid_type(py)?.call((), Some(&arguments))
}

/// `decimal.Decimal`.
pub fn decimal_type(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    DECIMAL_TYPE.import(py, "decimal", "Decimal")
}

/// The `datetime.timezone.utc` singleton - compared by identity with a value's own tzinfo.
pub fn utc_timezone(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    Ok(UTC_TIMEZONE
        .get_or_try_init(py, || -> PyResult<Py<PyAny>> {
            Ok(PyModule::import(py, "datetime")?.getattr("timezone")?.getattr("utc")?.unbind())
        })?
        .bind(py))
}
