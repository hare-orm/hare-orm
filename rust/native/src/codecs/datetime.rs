//! A `DatetimeField`. Reading converts the value to the configured zone (aware) or the system's
//! local time (naive), as `DatetimeField.get_python_value_with_timezone()` does; a value whose conversion
//! needs a rule only Python knows - a naive wall clock in a zone with DST, the system zone, an
//! infinity, a non-standard string - goes to the field's reader.

use chrono::{Datelike, Timelike};
use pyo3::exceptions::PyKeyError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDateAccess, PyDateTime, PyDict, PyString, PyTimeAccess, PyTzInfo, PyTzInfoAccess};
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::iso_text::{self, IsoDate, IsoDateTime, IsoTime};
use crate::codecs::options::Options;
use crate::python::references::PythonReferences;
use crate::python::{ffi, objects};

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

/// The ISO text SQLite stores for an aware datetime: its UTC wall clock, `+00:00`.
fn get_utc_text<'py>(value: &Bound<'py, PyDateTime>) -> PyResult<Bound<'py, PyAny>> {
    let py = value.py();
    let utc = objects::utc_timezone(py)?;
    let in_utc = match value.get_tzinfo() {
        Some(tzinfo) if tzinfo.is(utc) => value.clone(),
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

pub struct DatetimeWrite {
    fallback: Py<PyAny>,
    use_timezone: bool,
    /// The configured zone, under `use_timezone` - what an `auto_now` value is read back as.
    zone: Option<Py<PyAny>>,
    zone_is_utc: bool,
    /// The column stores UTC instants: without `use_timezone` a naive value is bound as the instant of its
    /// system-local wall clock.
    stores_utc_instants: bool,
    /// Bound as SQLite's ISO text rather than a datetime.
    text: bool,
    auto_now: bool,
    auto_now_add: bool,
    /// An `auto_now` value is stored on the instance directly - its `__setattr__` does nothing more
    /// for the field than drop a pending async default.
    assign_directly: bool,
    validate: Option<Py<PyAny>>,
    null: bool,
}

impl DatetimeWrite {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        let use_timezone = options.get_bool("use_timezone")?;
        Ok(DatetimeWrite {
            fallback: options.get_object("fallback")?,
            use_timezone,
            zone: if use_timezone { Some(options.get_object("zone")?) } else { None },
            zone_is_utc: options.get_bool("zone_is_utc")?,
            stores_utc_instants: options.get_bool("stores_utc_instants")?,
            text: options.get_bool("text")?,
            auto_now: options.get_bool("auto_now")?,
            auto_now_add: options.get_bool("auto_now_add")?,
            assign_directly: options.get_bool("assign_directly")?,
            validate: options.get_optional_object("validate")?,
            null: options.get_bool("null")?,
        })
    }

    /// Sets an `auto_now` value on the instance, as `instance.<field> = value` does.
    fn assign<'py>(
        &self,
        instance: &Bound<'py, PyAny>,
        field_name: &Bound<'py, PyString>,
        value: &Bound<'py, PyAny>,
    ) -> PyResult<()> {
        if !self.assign_directly {
            return instance.setattr(field_name, value);
        }
        let py = instance.py();
        if let Ok(pending_defaults) = instance.getattr(intern!(py, "_await_when_save")) {
            if let Ok(pending_defaults) = pending_defaults.cast::<PyDict>() {
                if !pending_defaults.is_empty() {
                    pending_defaults.del_item(field_name).or_else(|error| {
                        if error.is_instance_of::<PyKeyError>(py) {
                            Ok(())
                        } else {
                            Err(error)
                        }
                    })?;
                }
            }
        }
        ffi::generic_set_attribute(instance, field_name, value)
    }

    fn bound_value<'py>(&self, value: &Bound<'py, PyDateTime>) -> PyResult<Bound<'py, PyAny>> {
        if !self.text {
            return Ok(value.clone().into_any());
        }
        if value.get_tzinfo().is_none() {
            Ok(get_naive_text(value))
        } else {
            get_utc_text(value)
        }
    }

    /// The value bound for a naive datetime without `use_timezone` - None when the field converts it.
    fn get_local_bound_value<'py>(&self, value: &Bound<'py, PyDateTime>) -> Option<Bound<'py, PyDateTime>> {
        if !self.stores_utc_instants {
            return Some(value.clone());
        }
        // The instant of the system-local wall clock.
        value
            .call_method0(intern!(value.py(), "astimezone"))
            .ok()
            .and_then(|local| local.cast_into::<PyDateTime>().ok())
    }

    pub fn write<'py>(
        &self,
        value: Bound<'py, PyAny>,
        instance: &Bound<'py, PyAny>,
        field_name: &Bound<'py, PyString>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        let fallback = self.fallback.bind(py);
        if (self.auto_now || self.auto_now_add) && instance.hasattr(intern!(py, "_saved_in_db"))? {
            if !self.use_timezone && (self.auto_now || instance.getattr(field_name)?.is_none()) {
                // The system's naive local time, read back as it is.
                let now = objects::system_clock_type(py)?
                    .call_method0(intern!(py, "get_local_now"))?
                    .cast_into::<PyDateTime>()?;
                let Some(bound) = self.get_local_bound_value(&now) else {
                    return fallback.call1((value, instance));
                };
                self.assign(instance, field_name, &now)?;
                return self.bound_value(&bound);
            }
            if self.use_timezone && (self.auto_now || instance.getattr(field_name)?.is_none()) {
                let now = chrono::Utc::now().into_pyobject(py)?.cast_into::<PyDateTime>()?;
                let zone = self.zone.as_ref().expect("use_timezone carries a zone").bind(py);
                let read_back = if self.zone_is_utc {
                    with_tzinfo(&now, zone)?
                } else {
                    now.call_method1(intern!(py, "astimezone"), (zone,))?
                };
                self.assign(instance, field_name, &read_back)?;
                return self.bound_value(&now);
            }
        }
        if value.is_none() && self.null {
            return Ok(value);
        }
        let Ok(datetime) = value.cast_exact::<PyDateTime>() else {
            return fallback.call1((value, instance));
        };
        let year = datetime.get_year();
        if year == MINIMUM_YEAR || year == MAXIMUM_YEAR {
            return fallback.call1((value, instance));
        }
        let bound = match datetime.get_tzinfo() {
            // An aware value under use_timezone, as it is.
            Some(tzinfo) if self.use_timezone && objects::has_offset_for_datetime(&tzinfo)? => datetime.clone(),
            // A naive value without use_timezone: a wall clock, or the instant of it.
            None if !self.use_timezone => match self.get_local_bound_value(datetime) {
                Some(bound) => bound,
                None => return fallback.call1((value, instance)),
            },
            // An aware value without use_timezone: the system's local wall clock, or the instant itself.
            Some(tzinfo) if !self.use_timezone && objects::has_offset_for_datetime(&tzinfo)? => {
                let Some(local) = get_system_local_naive(datetime) else {
                    return fallback.call1((value, instance));
                };
                if self.stores_utc_instants {
                    datetime.clone()
                } else {
                    local.cast_into::<PyDateTime>()?
                }
            }
            // A naive value under use_timezone (warned about), another tzinfo - the field.
            _ => return fallback.call1((value, instance)),
        };
        if let Some(validate) = &self.validate {
            validate.bind(py).call1((&bound,))?;
        }
        self.bound_value(&bound)
    }
}

impl PythonReferences for DatetimeRead {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        visit.call(&self.zone)?;
        Ok(())
    }
}

impl PythonReferences for DatetimeWrite {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        visit.call(&self.zone)?;
        visit.call(&self.validate)?;
        Ok(())
    }
}
