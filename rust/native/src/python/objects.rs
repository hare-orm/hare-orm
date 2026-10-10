//! Python classes and objects the codecs use on every value, imported once per interpreter.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::PyType;

use crate::python::ffi;

static UUID_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static SAFE_UUID_UNKNOWN: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static DECIMAL_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static VALIDATION_ERROR_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static UTC_TIMEZONE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static TIMEZONE_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static ZONEINFO_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();
static SYSTEM_CLOCK_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();

/// `uuid.UUID`.
pub fn uuid_type(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    UUID_TYPE.import(py, "uuid", "UUID")
}

/// `uuid.SafeUUID.unknown` - what `uuid.UUID(int=...)` sets as `is_safe`.
pub fn safe_uuid_unknown(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    Ok(SAFE_UUID_UNKNOWN
        .get_or_try_init(py, || -> PyResult<Py<PyAny>> {
            Ok(PyModule::import(py, "uuid")?.getattr("SafeUUID")?.getattr("unknown")?.unbind())
        })?
        .bind(py))
}

/// The `uuid.UUID` of a 128-bit value - built as `UUID(int=...)` builds it, without its `__init__`.
pub fn new_uuid(py: Python<'_>, value: u128) -> PyResult<Bound<'_, PyAny>> {
    let uuid = ffi::allocate_instance(uuid_type(py)?)?;
    ffi::generic_set_attribute(&uuid, intern!(py, "int"), &value.into_pyobject(py)?.into_any())?;
    ffi::generic_set_attribute(&uuid, intern!(py, "is_safe"), safe_uuid_unknown(py)?)?;
    Ok(uuid)
}

/// A new instance of `class` holding `attributes`, built without its `__init__` - what a dataclass
/// whose `__init__` only stores its fields builds, frozen or not.
pub fn new_instance_with_attributes<'py>(
    class: &Bound<'py, PyType>,
    attributes: &[(&Bound<'py, pyo3::types::PyString>, &Bound<'py, PyAny>)],
) -> PyResult<Bound<'py, PyAny>> {
    let instance = ffi::allocate_instance(class)?;
    for (name, value) in attributes {
        ffi::generic_set_attribute(&instance, name, value)?;
    }
    Ok(instance)
}

/// `decimal.Decimal`.
pub fn decimal_type(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    DECIMAL_TYPE.import(py, "decimal", "Decimal")
}

/// `hare.exceptions.ValidationError`.
pub fn validation_error_type(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    VALIDATION_ERROR_TYPE.import(py, "hare.exceptions", "ValidationError")
}

/// The `datetime.timezone.utc` singleton - compared by identity with a value's own tzinfo.
pub fn utc_timezone(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    Ok(UTC_TIMEZONE
        .get_or_try_init(py, || -> PyResult<Py<PyAny>> {
            Ok(PyModule::import(py, "datetime")?.getattr("timezone")?.getattr("utc")?.unbind())
        })?
        .bind(py))
}

/// `hare.time.system_clock.SystemClock` - the clock every moment hare stamps is read from.
pub fn system_clock_type(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    SYSTEM_CLOCK_TYPE.import(py, "hare.time.system_clock", "SystemClock")
}

/// `datetime.timezone` - a fixed offset, reported for a bare time too.
pub fn timezone_type(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    TIMEZONE_TYPE.import(py, "datetime", "timezone")
}

/// `zoneinfo.ZoneInfo` - an offset for every datetime, none for a bare time.
pub fn zoneinfo_type(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    ZONEINFO_TYPE.import(py, "zoneinfo", "ZoneInfo")
}

/// Whether `tzinfo` reports an offset for any datetime - a `datetime.timezone` or a `ZoneInfo`; a
/// tzinfo of another class may report none, which makes its value naive.
pub fn has_offset_for_datetime(tzinfo: &Bound<'_, PyAny>) -> PyResult<bool> {
    // hare's own ZoneInfo subclass resolves offsets as ZoneInfo does.
    Ok(tzinfo.get_type().is(timezone_type(tzinfo.py())?) || tzinfo.is_instance(zoneinfo_type(tzinfo.py())?)?)
}
