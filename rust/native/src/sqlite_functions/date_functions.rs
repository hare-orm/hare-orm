//! The date part extraction and date truncation functions hare registers on SQLite (SQLite has no
//! `EXTRACT()` or `DATE_TRUNC()`), over the date and datetime text hare stores; a value in another
//! form, a time of day or an unknown part goes to the Python function.

use chrono::{Datelike, Duration, NaiveDate, NaiveDateTime, NaiveTime, Timelike};
use pyo3::prelude::*;
use pyo3::types::PyString;
use pyo3::{PyTraverseError, PyVisit};

use crate::sqlite_functions::iso_moment::{format_date, format_moment, format_time, IsoMoment};
use crate::sqlite_functions::zone_conversions::ZoneConversions;

/// The date functions; a value they don't read themselves goes to the Python ones given.
#[pyclass(frozen, module = "rust.native.sqlite_functions")]
pub struct DateFunctions {
    zones: ZoneConversions,
    /// `(date_part, value, zone_name)`, the Python extraction.
    extract_in_python: Py<PyAny>,
    /// `(trunc_type, value, zone_name)`, the Python truncation.
    truncate_in_python: Py<PyAny>,
}

/// The text of a str argument, None for anything else.
fn get_text<'a>(value: &'a Bound<'_, PyAny>) -> Option<&'a str> {
    value.cast::<PyString>().ok()?.to_str().ok()
}

/// `date` truncated to a calendar unit (`year`, `quarter`, `month`, `week`), itself for any other;
/// None below year 1.
fn truncate_date(trunc_type: &str, date: NaiveDate) -> Option<NaiveDate> {
    let truncated = match trunc_type {
        "year" => NaiveDate::from_ymd_opt(date.year(), 1, 1)?,
        "quarter" => NaiveDate::from_ymd_opt(date.year(), (date.month() - 1) / 3 * 3 + 1, 1)?,
        "month" => NaiveDate::from_ymd_opt(date.year(), date.month(), 1)?,
        "week" => date - Duration::days(i64::from(date.weekday().num_days_from_monday())),
        _ => date,
    };
    (truncated.year() >= 1).then_some(truncated)
}

/// `time` truncated to `hour`, `minute` or `second`.
fn truncate_time(trunc_type: &str, time: NaiveTime) -> NaiveTime {
    match trunc_type {
        "hour" => NaiveTime::from_hms_opt(time.hour(), 0, 0),
        "minute" => NaiveTime::from_hms_opt(time.hour(), time.minute(), 0),
        _ => NaiveTime::from_hms_opt(time.hour(), time.minute(), time.second()),
    }
    .expect("the fields of a valid time")
}

impl DateFunctions {
    /// A part of a moment's wall clock, as `DATE_PART_EXTRACTORS` gives it, or its `DATE`/`TIME`.
    fn get_part<'py>(py: Python<'py>, date_part: &str, moment: NaiveDateTime) -> PyResult<Option<Bound<'py, PyAny>>> {
        match date_part {
            "DATE" => Ok(Some(PyString::new(py, &format_date(moment.date())).into_any())),
            "TIME" => {
                let text = format!(
                    "{:02}:{:02}:{:02}.{:06}",
                    moment.hour(),
                    moment.minute(),
                    moment.second(),
                    moment.nanosecond() / 1000
                );
                Ok(Some(PyString::new(py, &text).into_any()))
            }
            _ => match get_date_part_number(date_part, moment) {
                Some(number) => Ok(Some(number.into_pyobject(py)?.into_any())),
                None => Ok(None),
            },
        }
    }
}

/// A numeric part of a moment's wall clock, as `DATE_PART_EXTRACTORS` gives it; None for another name.
pub fn get_date_part_number(date_part: &str, moment: NaiveDateTime) -> Option<i64> {
    let date = moment.date();
    Some(match date_part {
        "YEAR" => i64::from(date.year()),
        "ISOYEAR" => i64::from(date.iso_week().year()),
        "QUARTER" => i64::from((date.month() - 1) / 3 + 1),
        "MONTH" => i64::from(date.month()),
        "WEEK" => i64::from(date.iso_week().week()),
        "DOW" => i64::from(date.weekday().number_from_monday() % 7 + 1),
        "ISODOW" => i64::from(date.weekday().number_from_monday()),
        "DAY" => i64::from(date.day()),
        "HOUR" => i64::from(moment.hour()),
        "MINUTE" => i64::from(moment.minute()),
        "SECOND" => i64::from(moment.second()),
        "MICROSECOND" => i64::from(moment.nanosecond() / 1000),
        _ => return None,
    })
}

#[pymethods]
impl DateFunctions {
    #[new]
    fn new(zone_parser: Py<PyAny>, extract_in_python: Py<PyAny>, truncate_in_python: Py<PyAny>) -> Self {
        DateFunctions { zones: ZoneConversions::new(zone_parser), extract_in_python, truncate_in_python }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        self.zones.traverse(&visit)?;
        visit.call(&self.extract_in_python)?;
        visit.call(&self.truncate_in_python)
    }

    /// A date part of a stored date or datetime - in `zone_name` when it is given, a naive value
    /// read as UTC there.
    fn extract<'py>(
        &self,
        date_part: &Bound<'py, PyAny>,
        value: &Bound<'py, PyAny>,
        zone_name: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() {
            return Ok(py.None().into_bound(py));
        }
        if let (Some(part), Some(iso)) = (get_text(date_part), get_text(value).and_then(IsoMoment::parse)) {
            let moment = if get_text(zone_name).is_some_and(|name| !name.is_empty()) {
                let utc = iso.moment - Duration::seconds(i64::from(iso.offset_seconds.unwrap_or(0)));
                self.zones.get_wall_clock(utc, zone_name)?.map(|(moment, _fold)| moment)
            } else {
                Some(iso.moment)
            };
            if let Some(moment) = moment {
                if let Some(result) = Self::get_part(py, part, moment)? {
                    return Ok(result);
                }
            }
        }
        self.extract_in_python.bind(py).call1((date_part, value, zone_name))
    }

    /// A stored date or datetime truncated to a unit - an aware datetime in `zone_name`, given back
    /// in UTC.
    fn truncate<'py>(
        &self,
        trunc_type: &Bound<'py, PyAny>,
        value: &Bound<'py, PyAny>,
        zone_name: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() {
            return Ok(py.None().into_bound(py));
        }
        if let (Some(unit), Some(iso)) = (get_text(trunc_type), get_text(value).and_then(IsoMoment::parse)) {
            if let Some(text) = self.truncate_moment(unit, &iso, zone_name)? {
                return Ok(PyString::new(py, &text).into_any());
            }
        }
        self.truncate_in_python.bind(py).call1((trunc_type, value, zone_name))
    }
}

impl DateFunctions {
    /// The text `truncate()` gives for a moment; None when the Python function decides.
    fn truncate_moment(&self, unit: &str, iso: &IsoMoment, zone_name: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
        if iso.is_date {
            return Ok(truncate_date(unit, iso.moment.date()).map(format_date));
        }
        let in_zone = iso.offset_seconds.is_some() && get_text(zone_name).is_some_and(|name| !name.is_empty());
        let (local, fold) = if in_zone {
            let utc = iso.moment - Duration::seconds(i64::from(iso.offset_seconds.unwrap_or(0)));
            match self.zones.get_wall_clock(utc, zone_name)? {
                Some(local) => local,
                None => return Ok(None),
            }
        } else {
            (iso.moment, false)
        };
        // A truncated time of day keeps the wall clock's fold; a truncated date starts a new day.
        let mut keeps_fold = false;
        let wall_clock = match unit {
            "date" => return Ok(Some(format_date(local.date()))),
            "time" => return Ok(Some(format_time(local.time()))),
            "hour" | "minute" | "second" => {
                keeps_fold = fold;
                local.date().and_time(truncate_time(unit, local.time()))
            }
            _ => match truncate_date(unit, local.date()) {
                Some(date) => date.and_time(NaiveTime::MIN),
                None => return Ok(None),
            },
        };
        if !in_zone {
            return Ok(Some(format_moment(wall_clock, iso.offset_seconds)));
        }
        Ok(self.zones.get_utc_instant(wall_clock, keeps_fold, zone_name)?.map(|utc| format_moment(utc, Some(0))))
    }
}
