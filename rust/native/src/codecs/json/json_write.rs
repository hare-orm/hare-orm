use crate::codecs::options::Options;
use crate::python::references::PythonReferences;
use pyo3::types::PyString;
use pyo3::{PyTraverseError, PyVisit};

use super::encode;
use pyo3::prelude::*;

pub struct JsonWrite {
    validate: Option<Py<PyAny>>,
    null: bool,
    fallback: Py<PyAny>,
}

impl JsonWrite {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(JsonWrite {
            validate: options.get_optional_object("validate")?,
            null: options.get_bool("null")?,
            fallback: options.get_object("fallback")?,
        })
    }

    pub fn write<'py>(&self, value: Bound<'py, PyAny>, instance: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() && self.null {
            return Ok(value);
        }
        if value.is_none() {
            return self.fallback.bind(py).call1((value, instance));
        }
        let Some(text) = encode(&value) else {
            return self.fallback.bind(py).call1((value, instance));
        };
        if let Some(validate) = &self.validate {
            validate.bind(py).call1((&value,))?;
        }
        Ok(PyString::new(py, &text).into_any())
    }

    /// The text `write()` binds for `value`, without a Python str - None where `write()` binds
    /// something else (None, or a value the field encodes itself).
    pub fn write_text(&self, value: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
        if value.is_none() {
            return Ok(None);
        }
        let Some(text) = encode(value) else {
            return Ok(None);
        };
        if let Some(validate) = &self.validate {
            validate.bind(value.py()).call1((value,))?;
        }
        Ok(Some(text))
    }
}

impl PythonReferences for JsonWrite {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.validate)?;
        visit.call(&self.fallback)?;
        Ok(())
    }
}
