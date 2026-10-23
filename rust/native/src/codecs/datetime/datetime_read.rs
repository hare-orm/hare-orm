use crate::codecs::iso_text;
use crate::codecs::options::Options;
use crate::pg::error::{to_pyerr, DriverError};
use crate::python::objects;
use crate::python::references::PythonReferences;
use chrono::{Datelike, Timelike};
use pyo3::intern;
use pyo3::types::{PyDateAccess, PyDateTime, PyString, PyTzInfo, PyTzInfoAccess};
use pyo3::{PyTraverseError, PyVisit};

use super::{build_datetime, get_system_local_naive, is_naive_infinity, with_tzinfo, MAXIMUM_YEAR, MINIMUM_YEAR};
use pyo3::prelude::*;

pub struct DatetimeRead {
    fallback: Py<PyAny>,
    use_timezone: bool,
    /// The configured zone, under `use_timezone`.
    zone: Option<Py<PyAny>>,
    zone_is_utc: bool,
    /// A naive value is a UTC instant (a PostgreSQL `timestamp` read in a UTC session).
    naive_is_utc: bool,
}

impl DatetimeRead {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        let use_timezone = options.get_bool("use_timezone")?;
        Ok(DatetimeRead {
            fallback: options.get_object("fallback")?,
            use_timezone,
            zone: if use_timezone { Some(options.get_object("zone")?) } else { None },
            zone_is_utc: options.get_bool("zone_is_utc")?,
            naive_is_utc: options.get_bool("naive_is_utc")?,
        })
    }

    pub fn read<'py>(&self, raw: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = raw.py();
        if raw.is_none() {
            return Ok(raw);
        }
        if let Ok(value) = raw.cast::<PyDateTime>() {
            let value = value.clone();
            return self.convert(value, &raw, self.naive_is_utc);
        }
        if let Ok(text) = raw.cast::<PyString>() {
            let parsed = match text.to_str().ok().and_then(iso_text::parse_datetime) {
                Some(value) => build_datetime(py, value)?,
                // Anything else - checked and parsed as the field does it.
                None => return self.fallback.bind(py).call1((raw,)),
            };
            // Naive text is a wall clock, whatever a naive datetime from the driver is.
            return self.convert(parsed, &raw, false);
        }
        self.fallback.bind(py).call1((raw,))
    }

    /// The value of a UTC instant `rust.native.pg` read, built at once in the configured zone when
    /// that zone is UTC - what `read()` gives for it, without a datetime in between. None for any
    /// other configuration.
    pub fn read_utc_instant<'py>(
        &self,
        py: Python<'py>,
        instant: chrono::DateTime<chrono::Utc>,
    ) -> Option<PyResult<Bound<'py, PyAny>>> {
        if !self.use_timezone || !self.zone_is_utc {
            return None;
        }
        let zone = self.zone.as_ref()?.bind(py);
        let build = || -> PyResult<Bound<'py, PyAny>> {
            let wall_clock = instant.naive_utc();
            if !(1..=9999).contains(&wall_clock.year()) {
                return Err(to_pyerr(DriverError::Conversion(format!(
                    "timestamptz value {instant} is out of range for Python's datetime"
                ))));
            }
            Ok(PyDateTime::new(
                py,
                wall_clock.year(),
                wall_clock.month() as u8,
                wall_clock.day() as u8,
                wall_clock.hour() as u8,
                wall_clock.minute() as u8,
                wall_clock.second() as u8,
                wall_clock.nanosecond() / 1000,
                Some(zone.cast::<PyTzInfo>()?),
            )?
            .into_any())
        };
        Some(build())
    }

    /// The configured-zone value of `value`, a datetime read from `raw` - what the reader is given
    /// for a conversion left to the field.
    fn convert<'py>(
        &self,
        value: Bound<'py, PyDateTime>,
        raw: &Bound<'py, PyAny>,
        naive_is_utc: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        let utc = objects::utc_timezone(py)?;
        let tzinfo = value.get_tzinfo();
        if tzinfo.is_none() {
            if !self.use_timezone && !naive_is_utc {
                // Already the naive value the field holds.
                return Ok(value.into_any());
            }
            if is_naive_infinity(&value) {
                // An infinity - the field's own rules.
                return self.fallback.bind(py).call1((raw,));
            }
            if !self.use_timezone {
                // A UTC instant, read as the system's local time.
                let instant = with_tzinfo(&value, utc)?.cast_into::<PyDateTime>()?;
                return match get_system_local_naive(&instant) {
                    Some(local) => Ok(local),
                    None => self.fallback.bind(py).call1((raw,)),
                };
            }
            if naive_is_utc {
                return self.convert(with_tzinfo(&value, utc)?.cast_into::<PyDateTime>()?, raw, false);
            }
            // A naive wall clock in the configured zone: only UTC has no DST gap to check for.
            if self.zone_is_utc {
                return with_tzinfo(&value, self.zone.as_ref().expect("use_timezone carries a zone").bind(py));
            }
            return self.fallback.bind(py).call1((raw,));
        }
        let year = value.get_year();
        if !tzinfo.as_ref().is_some_and(|tzinfo| objects::has_offset_for_datetime(tzinfo).unwrap_or(false))
            || (!self.use_timezone && (year == MINIMUM_YEAR || year == MAXIMUM_YEAR))
        {
            // A tzinfo that may report no offset, or an infinity - the field's own rules.
            return self.fallback.bind(py).call1((raw,));
        }
        if !self.use_timezone {
            // The system's local time - the field knows the system zone even where Python's own
            // conversion fails (before 1970 on Windows).
            return match get_system_local_naive(&value) {
                Some(local) => Ok(local),
                None => self.fallback.bind(py).call1((raw,)),
            };
        }
        let zone = self.zone.as_ref().expect("use_timezone carries a zone").bind(py);
        if self.zone_is_utc && tzinfo.as_ref().is_some_and(|tzinfo| tzinfo.is(utc)) {
            // The same instant at offset 0 - only the tzinfo object changes.
            return with_tzinfo(&value, zone);
        }
        match value.call_method1(intern!(py, "astimezone"), (zone,)) {
            Ok(converted) => Ok(converted),
            // Out of range in the zone - the field raises its own ValidationError.
            Err(_) => self.fallback.bind(py).call1((raw,)),
        }
    }
}

impl PythonReferences for DatetimeRead {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        visit.call(&self.zone)?;
        Ok(())
    }
}
