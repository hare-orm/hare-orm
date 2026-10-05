//! A `DateField`: a date as it is, a datetime narrowed to its date, SQLite's ISO text parsed.

use pyo3::prelude::*;
use pyo3::types::{PyDate, PyDateAccess, PyDateTime, PyString};
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::iso_text::{self, IsoDate};
use crate::codecs::options::Options;
use crate::python::references::PythonReferences;

pub struct DateRead {
    fallback: Py<PyAny>,
}

impl DateRead {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(DateRead { fallback: options.get_object("fallback")? })
    }

    pub fn read<'py>(&self, raw: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = raw.py();
        if raw.is_none() || raw.is_exact_instance_of::<PyDate>() {
            return Ok(raw);
        }
        if let Ok(datetime) = raw.cast_exact::<PyDateTime>() {
            return Ok(PyDate::new(py, datetime.get_year(), datetime.get_month(), datetime.get_day())?.into_any());
        }
        if let Ok(text) = raw.cast::<PyString>() {
            if let Some(date) = text.to_str().ok().and_then(iso_text::parse_date) {
                return Ok(PyDate::new(py, date.year, date.month, date.day)?.into_any());
            }
        }
        self.fallback.bind(py).call1((raw,))
    }
}

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

impl PythonReferences for DateRead {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        Ok(())
    }
}

impl PythonReferences for DateWrite {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        visit.call(&self.validate)?;
        Ok(())
    }
}
