//! A `TimeField`: under `use_tz` a naive time gets the configured zone's fixed offset, without it an
//! aware time loses its tzinfo - only the tzinfo changes, never the wall clock.

use pyo3::prelude::*;
use pyo3::types::{PyDelta, PyString, PyTime, PyTimeAccess, PyTzInfo, PyTzInfoAccess};
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::datetime::get_fixed_offset_timezone;
use crate::codecs::iso_text;
use crate::codecs::options::Options;
use crate::python::objects;
use crate::python::references::PythonReferences;

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

pub struct TimeRead {
    fallback: Py<PyAny>,
    use_tz: bool,
    /// The configured zone's fixed offset, under `use_tz`.
    fixed_offset: Option<Py<PyTzInfo>>,
}

impl TimeRead {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        let use_tz = options.get_bool("use_tz")?;
        let fixed_offset = if use_tz {
            Some(options.get_object("fixed_offset")?.into_bound(options.py()).cast_into::<PyTzInfo>()?.unbind())
        } else {
            None
        };
        Ok(TimeRead { fallback: options.get_object("fallback")?, use_tz, fixed_offset })
    }

    fn convert<'py>(&self, time: Bound<'py, PyTime>) -> PyResult<Bound<'py, PyAny>> {
        let py = time.py();
        let Some(is_aware) = is_aware(&time)? else {
            return self.fallback.bind(py).call1((time,));
        };
        if self.use_tz && !is_aware {
            return with_tzinfo(&time, Some(self.fixed_offset.as_ref().expect("use_tz carries an offset").bind(py)));
        }
        if !self.use_tz && is_aware {
            return with_tzinfo(&time, None);
        }
        Ok(time.into_any())
    }

    pub fn read<'py>(&self, raw: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = raw.py();
        if raw.is_none() || raw.is_instance_of::<PyDelta>() {
            return Ok(raw);
        }
        if let Ok(time) = raw.cast::<PyTime>() {
            return self.convert(time.clone());
        }
        if let Ok(text) = raw.cast::<PyString>() {
            if let Some(parsed) = text.to_str().ok().and_then(iso_text::parse_time) {
                let tzinfo = match parsed.offset_seconds {
                    Some(offset_seconds) => {
                        Some(get_fixed_offset_timezone(py, offset_seconds)?.cast_into::<PyTzInfo>()?)
                    }
                    None => None,
                };
                let time =
                    PyTime::new(py, parsed.hour, parsed.minute, parsed.second, parsed.microsecond, tzinfo.as_ref())?;
                return self.convert(time);
            }
        }
        self.fallback.bind(py).call1((raw,))
    }
}

pub struct TimeWrite {
    fallback: Py<PyAny>,
    use_tz: bool,
    /// Bound as SQLite's ISO text rather than a time.
    text: bool,
    validate: Option<Py<PyAny>>,
    null: bool,
}

impl TimeWrite {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(TimeWrite {
            fallback: options.get_object("fallback")?,
            use_tz: options.get_bool("use_tz")?,
            text: options.get_bool("text")?,
            validate: options.get_optional_object("validate")?,
            null: options.get_bool("null")?,
        })
    }

    pub fn write<'py>(&self, value: Bound<'py, PyAny>, instance: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() && self.null {
            return Ok(value);
        }
        // A naive time under use_tz is warned about and a value of another type converted - by the
        // field.
        let Ok(time) = value.cast_exact::<PyTime>() else {
            return self.fallback.bind(py).call1((value, instance));
        };
        if is_aware(time)? != Some(self.use_tz) {
            return self.fallback.bind(py).call1((value, instance));
        }
        if let Some(validate) = &self.validate {
            validate.bind(py).call1((time,))?;
        }
        if self.text {
            return time.call_method0("isoformat");
        }
        Ok(value)
    }
}

impl PythonReferences for TimeRead {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        visit.call(&self.fixed_offset)?;
        Ok(())
    }
}

impl PythonReferences for TimeWrite {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        visit.call(&self.validate)?;
        Ok(())
    }
}
