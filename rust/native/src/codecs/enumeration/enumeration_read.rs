use crate::codecs::options::Options;
use crate::python::references::PythonReferences;
use pyo3::types::PyDict;
use pyo3::{PyTraverseError, PyVisit};

use pyo3::prelude::*;

pub struct EnumerationRead {
    /// `enum_type._value2member_map_`.
    members_by_value: Py<PyDict>,
    fallback: Py<PyAny>,
}

impl EnumerationRead {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(EnumerationRead {
            members_by_value: options.get_dict("members_by_value")?,
            fallback: options.get_object("fallback")?,
        })
    }

    pub fn read<'py>(&self, raw: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = raw.py();
        if raw.is_none() {
            return Ok(raw);
        }
        // A stored text of a non-str value, or an unhashable value - the field's own lookup.
        match self.members_by_value.bind(py).get_item(&raw) {
            Ok(Some(member)) => Ok(member),
            _ => self.fallback.bind(py).call1((raw,)),
        }
    }
}

impl PythonReferences for EnumerationRead {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.members_by_value)?;
        visit.call(&self.fallback)?;
        Ok(())
    }
}
