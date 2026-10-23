//! A `TimeField`: under `use_timezone` a naive time gets the configured zone's fixed offset, without it an
//! aware time loses its tzinfo - only the tzinfo changes, never the wall clock.

use crate::python::objects;
use pyo3::prelude::*;
use pyo3::types::{PyTime, PyTimeAccess, PyTzInfo, PyTzInfoAccess};

mod time_read;
mod time_write;

pub use time_read::TimeRead;
pub use time_write::TimeWrite;

/// `time` with `tzinfo` in place of its own.
fn with_tzinfo<'py>(time: &Bound<'py, PyTime>, tzinfo: Option<&Bound<'py, PyTzInfo>>) -> PyResult<Bound<'py, PyAny>> {
    Ok(PyTime::new_with_fold(
        time.py(),
        time.get_hour(),
        time.get_minute(),
        time.get_second(),
        time.get_microsecond(),
        tzinfo,
        time.get_fold(),
    )?
    .into_any())
}

/// Whether `time` is aware - None for a tzinfo other than `datetime.timezone`, which may report no
/// offset for a bare time (a `ZoneInfo` never does).
fn is_aware(time: &Bound<'_, PyTime>) -> PyResult<Option<bool>> {
    match time.get_tzinfo() {
        None => Ok(Some(false)),
        Some(tzinfo) if tzinfo.get_type().is(objects::timezone_type(time.py())?) => Ok(Some(true)),
        Some(_) => Ok(None),
    }
}
