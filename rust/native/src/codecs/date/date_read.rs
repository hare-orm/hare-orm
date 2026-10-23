use crate::codecs::iso_text;
use crate::codecs::options::Options;
use crate::python::references::PythonReferences;
use pyo3::types::{PyDate, PyDateAccess, PyDateTime, PyString};
use pyo3::{PyTraverseError, PyVisit};

use pyo3::prelude::*;

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

impl PythonReferences for DateRead {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        Ok(())
    }
}
