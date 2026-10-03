//! Conversions between a UTC instant and the wall clock of a named zone, through the Python zone
//! object of the name - its rules are the ones the Python functions apply.

use chrono::{Datelike, NaiveDate, NaiveDateTime, Timelike};
use pyo3::prelude::*;
use pyo3::types::{PyDateAccess, PyDateTime, PyString, PyTimeAccess, PyTzInfo};
use pyo3::PyVisit;

use crate::python::objects;

/// The zone whose conversions are done here - UTC has no offset at any moment.
const UTC_ZONE_NAME: &str = "UTC";

/// A datetime's fields.
pub fn read_fields(value: &Bound<'_, PyDateTime>) -> Option<NaiveDateTime> {
    NaiveDate::from_ymd_opt(value.get_year(), u32::from(value.get_month()), u32::from(value.get_day()))?
        .and_hms_micro_opt(
            u32::from(value.get_hour()),
            u32::from(value.get_minute()),
            u32::from(value.get_second()),
            value.get_microsecond(),
        )
}

/// The zones of names, made by the Python function given - as the Python functions make them.
pub struct ZoneConversions {
    /// `(zone_name) -> tzinfo`.
    zone_parser: Py<PyAny>,
}

impl ZoneConversions {
    pub fn new(zone_parser: Py<PyAny>) -> Self {
        ZoneConversions { zone_parser }
    }

    pub fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        visit.call(&self.zone_parser)
    }

    /// Whether a zone name is UTC itself.
    pub fn is_utc(zone_name: &Bound<'_, PyAny>) -> bool {
        zone_name.cast::<PyString>().ok().and_then(|name| name.to_str().ok()) == Some(UTC_ZONE_NAME)
    }

    /// The zone of a name.
    pub fn get_zone<'py>(&self, py: Python<'py>, zone_name: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyTzInfo>> {
        Ok(self.zone_parser.bind(py).call1((zone_name,))?.cast_into::<PyTzInfo>()?)
    }

    /// A UTC instant as an aware Python datetime in a zone.
    pub fn get_local<'py>(
        &self,
        utc: NaiveDateTime,
        zone_name: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyDateTime>> {
        let py = zone_name.py();
        let utc_zone = objects::utc_timezone(py)?.cast::<PyTzInfo>()?;
        let instant = PyDateTime::new(
            py,
            utc.year(),
            utc.month() as u8,
            utc.day() as u8,
            utc.hour() as u8,
            utc.minute() as u8,
            utc.second() as u8,
            utc.nanosecond() / 1000,
            Some(utc_zone),
        )?;
        Ok(instant.call_method1("astimezone", (self.get_zone(py, zone_name)?,))?.cast_into::<PyDateTime>()?)
    }

    /// The wall clock in a zone of a UTC instant, and whether it is the second of two equal wall
    /// clocks (`fold`); None when it falls outside the datetime range.
    pub fn get_wall_clock(
        &self,
        utc: NaiveDateTime,
        zone_name: &Bound<'_, PyAny>,
    ) -> PyResult<Option<(NaiveDateTime, bool)>> {
        if Self::is_utc(zone_name) {
            return Ok(Some((utc, false)));
        }
        let local = self.get_local(utc, zone_name)?;
        Ok(read_fields(&local).map(|moment| (moment, local.get_fold())))
    }

    /// The UTC instant of a wall clock in a zone, as `wall_clock.replace(tzinfo=zone)` with its
    /// `fold` reads it.
    pub fn get_utc_instant(
        &self,
        wall_clock: NaiveDateTime,
        fold: bool,
        zone_name: &Bound<'_, PyAny>,
    ) -> PyResult<Option<NaiveDateTime>> {
        if Self::is_utc(zone_name) {
            return Ok(Some(wall_clock));
        }
        let py = zone_name.py();
        let zone = self.get_zone(py, zone_name)?;
        let local = PyDateTime::new_with_fold(
            py,
            wall_clock.year(),
            wall_clock.month() as u8,
            wall_clock.day() as u8,
            wall_clock.hour() as u8,
            wall_clock.minute() as u8,
            wall_clock.second() as u8,
            wall_clock.nanosecond() / 1000,
            Some(&zone),
            fold,
        )?;
        let utc = local.call_method1("astimezone", (objects::utc_timezone(py)?,))?;
        Ok(read_fields(utc.cast::<PyDateTime>()?))
    }
}
