//! The stored text of a day's first moment, for comparing a date with a timestamp on SQLite - the
//! UTC text of the day's first moment in a zone, a naive midnight's text for no zone. A value not
//! starting with a `YYYY-MM-DD` date goes to the Python function.

use chrono::{NaiveDate, NaiveTime};
use pyo3::prelude::*;
use pyo3::types::PyString;
use pyo3::{PyTraverseError, PyVisit};

use crate::sqlite_functions::iso_moment::{format_moment, IsoMoment};
use crate::sqlite_functions::zone_conversions::ZoneConversions;

/// The date a text starts with, written `YYYY-MM-DD`.
fn read_leading_date(text: &str) -> Option<NaiveDate> {
    text.get(..10).and_then(IsoMoment::parse).map(|parsed| parsed.moment.date())
}

/// The day-start function; a value it doesn't read goes to the Python one given.
#[pyclass(frozen, module = "rust.native.sqlite_functions")]
pub struct DateTimestamp {
    zones: ZoneConversions,
    /// `(value, zone_name)`, the Python function.
    get_start_text_in_python: Py<PyAny>,
}

#[pymethods]
impl DateTimestamp {
    #[new]
    fn new(zone_parser: Py<PyAny>, zones_by_name: Py<PyAny>, get_start_text_in_python: Py<PyAny>) -> Self {
        DateTimestamp { zones: ZoneConversions::new(zone_parser, zones_by_name), get_start_text_in_python }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        self.zones.traverse(&visit)?;
        visit.call(&self.get_start_text_in_python)
    }

    /// The UTC text of the first moment of a date's day in `zone_name`, e.g.
    /// `2020-01-01 15:00:00+00:00`; a naive midnight's text for no zone; None for NULL.
    fn get_start_text<'py>(
        &self,
        value: &Bound<'py, PyAny>,
        zone_name: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() {
            return Ok(py.None().into_bound(py));
        }
        let date = value.cast::<PyString>().ok().and_then(|text| read_leading_date(text.to_str().ok()?));
        if let Some(date) = date {
            let midnight = date.and_time(NaiveTime::MIN);
            if zone_name.is_none() {
                return Ok(PyString::new(py, &format_moment(midnight, None)).into_any());
            }
            // A wall clock inside a gap read with the offset before it is the instant the gap ends.
            if zone_name.is_instance_of::<PyString>() {
                if let Ok(Some(utc)) = self.zones.get_utc_instant(midnight, false, zone_name) {
                    return Ok(PyString::new(py, &format_moment(utc, Some(0))).into_any());
                }
            }
        }
        self.get_start_text_in_python.bind(py).call1((value, zone_name))
    }
}
