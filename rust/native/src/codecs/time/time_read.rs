use crate::codecs::datetime::get_fixed_offset_timezone;
use crate::codecs::iso_text;
use crate::codecs::options::Options;
use crate::python::references::PythonReferences;
use pyo3::types::{PyDelta, PyString, PyTime, PyTzInfo};
use pyo3::{PyTraverseError, PyVisit};

use super::{is_aware, with_tzinfo};
use pyo3::prelude::*;

pub struct TimeRead {
    fallback: Py<PyAny>,
    use_timezone: bool,
    /// The configured zone's fixed offset, under `use_timezone`.
    fixed_offset: Option<Py<PyTzInfo>>,
}

impl TimeRead {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        let use_timezone = options.get_bool("use_timezone")?;
        let fixed_offset = if use_timezone {
            Some(options.get_object("fixed_offset")?.into_bound(options.py()).cast_into::<PyTzInfo>()?.unbind())
        } else {
            None
        };
        Ok(TimeRead { fallback: options.get_object("fallback")?, use_timezone, fixed_offset })
    }

    fn convert<'py>(&self, time: Bound<'py, PyTime>) -> PyResult<Bound<'py, PyAny>> {
        let py = time.py();
        let Some(is_aware) = is_aware(&time)? else {
            return self.fallback.bind(py).call1((time,));
        };
        if self.use_timezone && !is_aware {
            return with_tzinfo(
                &time,
                Some(self.fixed_offset.as_ref().expect("use_timezone carries an offset").bind(py)),
            );
        }
        if !self.use_timezone && is_aware {
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

impl PythonReferences for TimeRead {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        visit.call(&self.fixed_offset)?;
        Ok(())
    }
}
