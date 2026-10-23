use crate::codecs::options::Options;
use crate::python::objects;
use crate::python::references::PythonReferences;
use pyo3::intern;
use pyo3::types::PyString;
use pyo3::{PyTraverseError, PyVisit};

use super::format_uuid;
use pyo3::prelude::*;

pub struct UuidWrite {
    fallback: Py<PyAny>,
    validate: Option<Py<PyAny>>,
    null: bool,
    /// Whether a UUID is written as itself, for a driver binding UUIDs - else as its text.
    as_object: bool,
}

impl UuidWrite {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(UuidWrite {
            fallback: options.get_object("fallback")?,
            validate: options.get_optional_object("validate")?,
            null: options.get_bool("null")?,
            as_object: options.get_bool("as_object")?,
        })
    }

    pub fn write<'py>(&self, value: Bound<'py, PyAny>, instance: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() && self.null {
            return Ok(value);
        }
        if !value.get_type().is(objects::uuid_type(py)?) {
            return self.fallback.bind(py).call1((value, instance));
        }
        if let Some(validate) = &self.validate {
            validate.bind(py).call1((&value,))?;
        }
        if self.as_object {
            return Ok(value);
        }
        let integer: u128 = value.getattr(intern!(py, "int"))?.extract()?;
        Ok(PyString::new(py, &format_uuid(integer)).into_any())
    }
}

impl PythonReferences for UuidWrite {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        visit.call(&self.validate)?;
        Ok(())
    }
}
