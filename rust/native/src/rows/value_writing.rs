//! Conversions of written values that Python and the column writers share - the one implementation of each.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBytes, PyDict, PyString};

/// `orjson.dumps`, imported on the first encode.
static ORJSON_DUMPS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// The canonical text of a UUID (`8-4-4-4-12` lowercase hex digits) - what `str()` of a `uuid.UUID`
/// gives, made from its `int`.
#[pyfunction]
pub fn write_uuid_text<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyString>> {
    let number: u128 = value.getattr(intern!(value.py(), "int"))?.extract()?;
    Ok(PyString::new(value.py(), &format_uuid(number)))
}

/// `number` as the canonical text of a UUID.
pub fn format_uuid(number: u128) -> String {
    format!(
        "{:08x}-{:04x}-{:04x}-{:04x}-{:012x}",
        (number >> 96) as u32,
        ((number >> 80) & 0xffff) as u16,
        ((number >> 64) & 0xffff) as u16,
        ((number >> 48) & 0xffff) as u16,
        number & 0xffff_ffff_ffff
    )
}

/// The microseconds of a `timedelta` - `(days * 86400 + seconds) * 1_000_000 + microseconds`, an int of
/// any size.
#[pyfunction]
pub fn get_timedelta_microseconds<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = value.py();
    let microseconds = count_microseconds(value)?;
    Ok(microseconds.into_pyobject(py)?.into_any())
}

/// The microseconds of a `timedelta`.
pub fn count_microseconds(value: &Bound<'_, PyAny>) -> PyResult<i128> {
    let py = value.py();
    let days: i128 = value.getattr(intern!(py, "days"))?.extract()?;
    let seconds: i128 = value.getattr(intern!(py, "seconds"))?.extract()?;
    let microseconds: i128 = value.getattr(intern!(py, "microseconds"))?.extract()?;
    Ok((days * 86_400 + seconds) * 1_000_000 + microseconds)
}

/// Encodes `value` as compact JSON text with orjson - `orjson.dumps(value, default=default).decode()`.
/// What orjson refuses raises its error.
#[pyfunction]
pub fn dumps_json<'py>(value: &Bound<'py, PyAny>, default: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyString>> {
    let py = value.py();
    let dumps = ORJSON_DUMPS.get_or_try_init(py, || py.import("orjson")?.getattr("dumps").map(Bound::unbind))?;
    let options = PyDict::new(py);
    options.set_item(intern!(py, "default"), default)?;
    let encoded = dumps.bind(py).call((value,), Some(&options))?.cast_into::<PyBytes>()?;
    // orjson writes UTF-8 only.
    let text = std::str::from_utf8(encoded.as_bytes())
        .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))?;
    Ok(PyString::new(py, text))
}
