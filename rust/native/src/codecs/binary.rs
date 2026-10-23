//! Reading a `BinaryField`: bytes as they are.

use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::options::Options;
use crate::python::references::PythonReferences;

pub struct BinaryRead {
    fallback: Py<PyAny>,
}

impl BinaryRead {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(BinaryRead { fallback: options.get_object("fallback")? })
    }

    pub fn read<'py>(&self, raw: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        if raw.is_none() || raw.is_exact_instance_of::<PyBytes>() {
            return Ok(raw);
        }
        self.fallback.bind(raw.py()).call1((raw,))
    }
}

impl PythonReferences for BinaryRead {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        Ok(())
    }
}
