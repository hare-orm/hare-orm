use crate::codecs::iso_text::{self, IsoDate};
use crate::codecs::options::Options;
use crate::python::references::PythonReferences;
use pyo3::types::{PyDate, PyDateAccess, PyString};
use pyo3::{PyTraverseError, PyVisit};

use pyo3::prelude::*;

pub struct DateWrite {
    fallback: Py<PyAny>,
    /// Bound as SQLite's ISO text rather than a date.
    text: bool,
    validate: Option<Py<PyAny>>,
    null: bool,
}

impl DateWrite {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(DateWrite {
            fallback: options.get_object("fallback")?,
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
        let Ok(date) = value.cast_exact::<PyDate>() else {
            return self.fallback.bind(py).call1((value, instance));
        };
        if let Some(validate) = &self.validate {
            validate.bind(py).call1((date,))?;
        }
        if !self.text {
            return Ok(value);
        }
        let mut text = String::with_capacity(10);
        iso_text::format_date(
            IsoDate { year: date.get_year(), month: date.get_month(), day: date.get_day() },
            &mut text,
        );
        Ok(PyString::new(py, &text).into_any())
    }
}

impl PythonReferences for DateWrite {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        visit.call(&self.validate)?;
        Ok(())
    }
}
