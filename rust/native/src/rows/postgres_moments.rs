//! The binary `date` and `timestamptz` values of an asyncpg connection - asyncpg's own codecs, except
//! that `date.max`/`datetime.max` and the minimums are written as the day and the moment they are, as
//! the native driver writes them, rather than as `infinity`. An `infinity` read is still the Python
//! extreme asyncpg reads it as.

use bytes::BytesMut;
use chrono::{DateTime, NaiveDate, NaiveTime, Utc};
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDate, PyDateTime};
use tokio_postgres::types::{FromSql, Type};

use crate::pg::types::datetime::{decode_date, python_max_datetime, python_min_datetime, timestamp_infinity};
use crate::pg::value::Value;

/// The binary payload of `value` as a `postgres_type` parameter.
fn encode<'py>(py: Python<'py>, value: &Value, postgres_type: &Type) -> PyResult<Bound<'py, PyBytes>> {
    let mut out = BytesMut::new();
    value.encode(postgres_type, &mut out, true).map_err(|error| PyValueError::new_err(error.to_string()))?;
    Ok(PyBytes::new(py, &out))
}

/// The error of a value that is no date, in asyncpg's words.
fn not_a_date(value: &Bound<'_, PyAny>) -> PyErr {
    let type_name = value.get_type().name().map(|name| name.to_string()).unwrap_or_default();
    PyTypeError::new_err(format!("expected a datetime.date or datetime.datetime instance, got {type_name:?}"))
}

/// A binary `date`: `date.max`/`date.min` for `infinity`/`-infinity`.
#[pyfunction]
pub fn read_postgres_date<'py>(py: Python<'py>, value: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let date = decode_date(value).map_err(|error| PyValueError::new_err(error.to_string()))?;
    Ok(date.into_pyobject(py)?.into_any())
}

/// The binary `date` of a date - of a datetime, its date.
#[pyfunction]
pub fn write_postgres_date<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyBytes>> {
    if !value.is_instance_of::<PyDate>() {
        return Err(not_a_date(value));
    }
    encode(value.py(), &Value::Date(value.extract::<NaiveDate>()?), &Type::DATE)
}

/// A binary `timestamptz`: an aware datetime in UTC, the naive `datetime.max`/`datetime.min` for
/// `infinity`/`-infinity`.
#[pyfunction]
pub fn read_postgres_timestamptz<'py>(py: Python<'py>, value: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    match timestamp_infinity(value) {
        Some(true) => Ok(python_max_datetime().into_pyobject(py)?.into_any()),
        Some(false) => Ok(python_min_datetime().into_pyobject(py)?.into_any()),
        None => {
            let moment = DateTime::<Utc>::from_sql(&Type::TIMESTAMPTZ, value)
                .map_err(|error| PyValueError::new_err(error.to_string()))?;
            Ok(moment.into_pyobject(py)?.into_any())
        }
    }
}

/// The binary `timestamptz` of a datetime - a naive one, and a date's midnight, as the machine's local
/// time.
#[pyfunction]
pub fn write_postgres_timestamptz<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyBytes>> {
    let parameter = if value.is_instance_of::<PyDateTime>() {
        value.extract::<Value>()?
    } else if value.is_instance_of::<PyDate>() {
        Value::Timestamp(value.extract::<NaiveDate>()?.and_time(NaiveTime::MIN))
    } else {
        return Err(not_a_date(value));
    };
    encode(value.py(), &parameter, &Type::TIMESTAMPTZ)
}
