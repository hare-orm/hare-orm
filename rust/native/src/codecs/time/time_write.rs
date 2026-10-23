use crate::codecs::options::Options;
use crate::python::references::PythonReferences;
use pyo3::intern;
use pyo3::types::PyTime;
use pyo3::{PyTraverseError, PyVisit};

use super::is_aware;
use pyo3::prelude::*;

pub struct TimeWrite {
    fallback: Py<PyAny>,
    use_timezone: bool,
    /// Bound as SQLite's ISO text rather than a time.
    text: bool,
    validate: Option<Py<PyAny>>,
    null: bool,
}

impl TimeWrite {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(TimeWrite {
            fallback: options.get_object("fallback")?,
            use_timezone: options.get_bool("use_timezone")?,
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
        // A naive time under use_timezone is warned about and a value of another type converted - by the
        // field.
        let Ok(time) = value.cast_exact::<PyTime>() else {
            return self.fallback.bind(py).call1((value, instance));
        };
        if is_aware(time)? != Some(self.use_timezone) {
            return self.fallback.bind(py).call1((value, instance));
        }
        if let Some(validate) = &self.validate {
            validate.bind(py).call1((time,))?;
        }
        if self.text {
            return time.call_method0(intern!(time.py(), "isoformat"));
        }
        Ok(value)
    }
}

impl PythonReferences for TimeWrite {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        visit.call(&self.validate)?;
        Ok(())
    }
}
