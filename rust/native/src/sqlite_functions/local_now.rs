//! The current date or wall-clock time in a zone with DST rules, which SQLite's own date functions
//! can't apply - a `Now()` default of a date or time column. A time takes the zone's standard
//! offset; a zone reporting none, or one with seconds, goes to the Python function.

use std::time::{SystemTime, UNIX_EPOCH};

use chrono::{DateTime, NaiveDateTime};
use pyo3::prelude::*;
use pyo3::types::{PyDelta, PyDeltaAccess, PyString};
use pyo3::{PyTraverseError, PyVisit};

use crate::sqlite_functions::iso_moment::{format_date, format_time};
use crate::sqlite_functions::zone_conversions::{read_fields, ZoneConversions};

/// The `value_type` of a date.
const DATE_VALUE_TYPE: &str = "date";

/// The current UTC instant.
fn get_utc_now() -> Option<NaiveDateTime> {
    let elapsed = SystemTime::now().duration_since(UNIX_EPOCH).ok()?;
    let microseconds = i64::try_from(elapsed.as_micros()).ok()?;
    DateTime::from_timestamp_micros(microseconds).map(|moment| moment.naive_utc())
}

/// Whole seconds of a timedelta; None for one with microseconds.
fn get_delta_seconds(delta: &Bound<'_, PyDelta>) -> Option<i64> {
    (delta.get_microseconds() == 0).then(|| i64::from(delta.get_days()) * 86_400 + i64::from(delta.get_seconds()))
}

/// The local-now function; arguments it doesn't handle go to the Python one given.
#[pyclass(frozen, module = "rust.native.sqlite_functions")]
pub struct LocalNow {
    zones: ZoneConversions,
    /// `(zone_name, value_type)`, the Python function.
    get_local_now_text_in_python: Py<PyAny>,
}

#[pymethods]
impl LocalNow {
    #[new]
    fn new(zone_parser: Py<PyAny>, get_local_now_text_in_python: Py<PyAny>) -> Self {
        LocalNow { zones: ZoneConversions::new(zone_parser), get_local_now_text_in_python }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        self.zones.traverse(&visit)?;
        visit.call(&self.get_local_now_text_in_python)
    }

    /// `YYYY-MM-DD` of today in the zone, or `HH:MM:SS[.ffffff]` of now there with the zone's
    /// standard offset.
    fn get_local_now_text<'py>(
        &self,
        zone_name: &Bound<'py, PyAny>,
        value_type: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = zone_name.py();
        let value_type_text = value_type.cast::<PyString>().ok().and_then(|text| text.to_str().ok());
        if let (true, Some(value_type_text), Some(utc)) =
            (zone_name.is_instance_of::<PyString>(), value_type_text, get_utc_now())
        {
            if let Some(text) = self.get_text(utc, zone_name, value_type_text)? {
                return Ok(PyString::new(py, &text).into_any());
            }
        }
        self.get_local_now_text_in_python.bind(py).call1((zone_name, value_type))
    }
}

impl LocalNow {
    /// The text of a UTC instant in a zone; None when the Python function decides.
    fn get_text(&self, utc: NaiveDateTime, zone_name: &Bound<'_, PyAny>, value_type: &str) -> PyResult<Option<String>> {
        let local = self.zones.get_local(utc, zone_name)?;
        let Some(wall_clock) = read_fields(&local) else {
            return Ok(None);
        };
        if value_type == DATE_VALUE_TYPE {
            return Ok(Some(format_date(wall_clock.date())));
        }
        let Some(offset_seconds) = local.call_method0("utcoffset")?.cast::<PyDelta>().ok().and_then(get_delta_seconds)
        else {
            return Ok(None);
        };
        let dst = local.call_method0("dst")?;
        let dst_seconds = match dst.cast::<PyDelta>() {
            Ok(dst) => match get_delta_seconds(dst) {
                Some(seconds) => seconds,
                None => return Ok(None),
            },
            Err(_) if dst.is_none() => 0,
            Err(_) => return Ok(None),
        };
        let standard_offset = offset_seconds - dst_seconds;
        if standard_offset % 60 != 0 || standard_offset.abs() >= 86_400 {
            return Ok(None);
        }
        let sign = if standard_offset < 0 { '-' } else { '+' };
        let minutes = standard_offset.abs() / 60;
        Ok(Some(format!("{}{sign}{:02}:{:02}", format_time(wall_clock.time()), minutes / 60, minutes % 60)))
    }
}
