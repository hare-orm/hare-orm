//! A `DatetimeField`. Reading converts the value to the configured zone (aware) or the system's
//! local time (naive), as `DatetimeField.get_python_value_with_timezone()` does; a value whose conversion
//! needs a rule only Python knows - a naive wall clock in a zone with DST, the system zone, an
//! infinity, a non-standard string - goes to the field's reader.

use crate::codecs::iso_text::{self, IsoDate, IsoDateTime, IsoTime};
use crate::python::objects;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDateAccess, PyDateTime, PyDelta, PyDeltaAccess, PyString, PyTimeAccess, PyTzInfo, PyTzInfoAccess};

mod datetime_read;
mod datetime_write;

pub use datetime_read::DatetimeRead;
pub use datetime_write::DatetimeWrite;

/// The year of `datetime.min`.
const MINIMUM_YEAR: i32 = 1;

/// The year of `datetime.max`.
const MAXIMUM_YEAR: i32 = 9999;

/// Whether a naive `value` is `datetime.max` or `datetime.min` - an infinity, not a wall clock.
fn is_naive_infinity(value: &Bound<'_, PyDateTime>) -> bool {
    let is_maximum = value.get_year() == MAXIMUM_YEAR
        && value.get_month() == 12
        && value.get_day() == 31
        && value.get_hour() == 23
        && value.get_minute() == 59
        && value.get_second() == 59
        && value.get_microsecond() == 999_999;
    let is_minimum = value.get_year() == MINIMUM_YEAR
        && value.get_month() == 1
        && value.get_day() == 1
        && value.get_hour() == 0
        && value.get_minute() == 0
        && value.get_second() == 0
        && value.get_microsecond() == 0;
    is_maximum || is_minimum
}

/// `value` with `tzinfo` in place of its own, the wall clock unchanged.
pub fn with_tzinfo<'py>(value: &Bound<'py, PyDateTime>, tzinfo: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    Ok(PyDateTime::new_with_fold(
        value.py(),
        value.get_year(),
        value.get_month(),
        value.get_day(),
        value.get_hour(),
        value.get_minute(),
        value.get_second(),
        value.get_microsecond(),
        Some(tzinfo.cast::<PyTzInfo>()?),
        value.get_fold(),
    )?
    .into_any())
}

/// The `datetime.timezone` of a UTC offset in seconds - the `timezone.utc` singleton for zero, as
/// `datetime.fromisoformat()` gives it.
pub fn get_fixed_offset_timezone(py: Python<'_>, offset_seconds: i32) -> PyResult<Bound<'_, PyAny>> {
    if offset_seconds == 0 {
        return Ok(objects::utc_timezone(py)?.clone());
    }
    let offset = chrono::FixedOffset::east_opt(offset_seconds).ok_or_else(|| {
        pyo3::exceptions::PyValueError::new_err(format!("offset of {offset_seconds}s is out of range"))
    })?;
    Ok(offset.into_pyobject(py)?.into_any())
}

/// The datetime of parsed ISO text.
fn build_datetime(py: Python<'_>, value: IsoDateTime) -> PyResult<Bound<'_, PyDateTime>> {
    let tzinfo = match value.time.offset_seconds {
        Some(offset_seconds) => Some(get_fixed_offset_timezone(py, offset_seconds)?.cast_into::<PyTzInfo>()?),
        None => None,
    };
    PyDateTime::new(
        py,
        value.date.year,
        value.date.month,
        value.date.day,
        value.time.hour,
        value.time.minute,
        value.time.second,
        value.time.microsecond,
        tzinfo.as_ref(),
    )
}

/// The system's local wall clock of an aware datetime, naive - None when Python can't convert it
/// (before 1970 on Windows), which the field does with its own fallback zone.
fn get_system_local_naive<'py>(value: &Bound<'py, PyDateTime>) -> Option<Bound<'py, PyAny>> {
    let local = value.call_method0(intern!(value.py(), "astimezone")).ok()?.cast_into::<PyDateTime>().ok()?;
    without_tzinfo(&local).ok()
}

/// `value` without its tzinfo, the wall clock unchanged.
fn without_tzinfo<'py>(value: &Bound<'py, PyDateTime>) -> PyResult<Bound<'py, PyAny>> {
    Ok(PyDateTime::new_with_fold(
        value.py(),
        value.get_year(),
        value.get_month(),
        value.get_day(),
        value.get_hour(),
        value.get_minute(),
        value.get_second(),
        value.get_microsecond(),
        None,
        value.get_fold(),
    )?
    .into_any())
}

/// The ISO text SQLite stores for a naive datetime: its wall clock, without an offset.
fn get_naive_text<'py>(value: &Bound<'py, PyDateTime>) -> Bound<'py, PyAny> {
    let text = iso_text::format_datetime(IsoDateTime {
        date: IsoDate { year: value.get_year(), month: value.get_month(), day: value.get_day() },
        time: IsoTime {
            hour: value.get_hour(),
            minute: value.get_minute(),
            second: value.get_second(),
            microsecond: value.get_microsecond(),
            offset_seconds: None,
        },
    });
    PyString::new(value.py(), &text).into_any()
}

/// Whether an aware datetime is at no offset from UTC.
fn has_zero_offset(value: &Bound<'_, PyDateTime>) -> PyResult<bool> {
    let offset = value.call_method0(intern!(value.py(), "utcoffset"))?;
    let Ok(offset) = offset.cast::<PyDelta>() else {
        return Ok(false);
    };
    Ok(offset.get_days() == 0 && offset.get_seconds() == 0 && offset.get_microseconds() == 0)
}

/// The ISO text SQLite stores for an aware datetime: its UTC wall clock, `+00:00`.
fn get_utc_text<'py>(value: &Bound<'py, PyDateTime>) -> PyResult<Bound<'py, PyAny>> {
    let py = value.py();
    let utc = objects::utc_timezone(py)?;
    let in_utc = match value.get_tzinfo() {
        Some(tzinfo) if tzinfo.is(utc) => value.clone(),
        // A zone of no offset at this moment (`ZoneInfo("UTC")`, which a read gives back) - its wall
        // clock is the UTC one.
        Some(_) if has_zero_offset(value)? => value.clone(),
        _ => value.call_method1(intern!(py, "astimezone"), (utc,))?.cast_into::<PyDateTime>()?,
    };
    let text = iso_text::format_datetime(IsoDateTime {
        date: IsoDate { year: in_utc.get_year(), month: in_utc.get_month(), day: in_utc.get_day() },
        time: IsoTime {
            hour: in_utc.get_hour(),
            minute: in_utc.get_minute(),
            second: in_utc.get_second(),
            microsecond: in_utc.get_microsecond(),
            offset_seconds: Some(0),
        },
    });
    Ok(PyString::new(py, &text).into_any())
}
