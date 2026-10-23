use crate::codecs::options::Options;
use crate::python::objects;
use crate::python::references::PythonReferences;
use pyo3::intern;
use pyo3::types::{PyDateAccess, PyDateTime, PyTzInfoAccess};
use pyo3::{PyTraverseError, PyVisit};

use super::{get_naive_text, get_system_local_naive, get_utc_text, MAXIMUM_YEAR, MINIMUM_YEAR};
use pyo3::prelude::*;

pub struct DatetimeWrite {
    fallback: Py<PyAny>,
    use_timezone: bool,
    /// The column stores UTC instants: without `use_timezone` a naive value is bound as the instant of its
    /// system-local wall clock.
    stores_utc_instants: bool,
    /// Bound as SQLite's ISO text rather than a datetime.
    text: bool,
    validate: Option<Py<PyAny>>,
    null: bool,
}

impl DatetimeWrite {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(DatetimeWrite {
            fallback: options.get_object("fallback")?,
            use_timezone: options.get_bool("use_timezone")?,
            stores_utc_instants: options.get_bool("stores_utc_instants")?,
            text: options.get_bool("text")?,
            validate: options.get_optional_object("validate")?,
            null: options.get_bool("null")?,
        })
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

    pub fn write<'py>(&self, value: Bound<'py, PyAny>, instance: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        let fallback = self.fallback.bind(py);
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

impl PythonReferences for DatetimeWrite {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        visit.call(&self.validate)?;
        Ok(())
    }
}
