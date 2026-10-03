//! Reading a `BooleanField`: a bool as it is, SQLite's 0/1 as a bool.

use pyo3::prelude::*;
use pyo3::types::{PyBool, PyInt};
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::options::Options;
use crate::python::references::PythonReferences;

pub struct BooleanRead {
    fallback: Py<PyAny>,
}

impl BooleanRead {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(BooleanRead { fallback: options.get_object("fallback")? })
    }

    pub fn read<'py>(&self, raw: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = raw.py();
        if raw.is_none() || raw.is_instance_of::<PyBool>() {
            return Ok(raw);
        }
        if raw.is_exact_instance_of::<PyInt>() {
            return Ok(PyBool::new(py, raw.is_truthy()?).to_owned().into_any());
        }
        self.fallback.bind(py).call1((raw,))
    }
}

impl PythonReferences for BooleanRead {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        Ok(())
    }
}
