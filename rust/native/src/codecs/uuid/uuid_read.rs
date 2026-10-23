use crate::codecs::options::Options;
use crate::python::objects;
use crate::python::references::PythonReferences;
use pyo3::intern;
use pyo3::types::PyString;
use pyo3::{PyTraverseError, PyVisit};

use super::parse_uuid;
use pyo3::prelude::*;

pub struct UuidRead {
    fallback: Py<PyAny>,
}

impl UuidRead {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(UuidRead { fallback: options.get_object("fallback")? })
    }

    pub fn read<'py>(&self, raw: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = raw.py();
        let uuid_type = objects::uuid_type(py)?;
        if raw.is_none() || raw.get_type().is(uuid_type) {
            return Ok(raw);
        }
        if raw.is_instance(uuid_type)? {
            // A driver's own subclass - a plain UUID of the same value.
            return objects::new_uuid(py, raw.getattr(intern!(py, "int"))?.extract()?);
        }
        if let Ok(text) = raw.cast::<PyString>() {
            if let Some(value) = text.to_str().ok().and_then(parse_uuid) {
                return objects::new_uuid(py, value);
            }
        }
        self.fallback.bind(py).call1((raw,))
    }
}

impl PythonReferences for UuidRead {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        Ok(())
    }
}
