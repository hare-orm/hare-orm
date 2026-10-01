//! A PostgreSQL array field: a list read element by element through the element field's codec, a
//! NULL element kept None.

use pyo3::prelude::*;
use pyo3::types::PyList;
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::options::Options;
use crate::codecs::read_codec::ReadCodec;
use crate::python::references::PythonReferences;

pub struct ArrayRead {
    element: Box<ReadCodec>,
    fallback: Py<PyAny>,
}

impl ArrayRead {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        let element_kind = options.get_string("element_kind")?;
        let element = ReadCodec::build(&element_kind, &options.get_nested("element_options")?)?;
        Ok(ArrayRead { element: Box::new(element), fallback: options.get_object("fallback")? })
    }

    pub fn read<'py>(&self, raw: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = raw.py();
        if raw.is_none() {
            return Ok(raw);
        }
        // A tuple, or a string the field refuses - the field's own reading.
        let Ok(elements) = raw.cast_exact::<PyList>() else {
            return self.fallback.bind(py).call1((raw,));
        };
        let values = PyList::empty(py);
        for element in elements.iter() {
            values.append(if element.is_none() { element } else { self.element.read(element)? })?;
        }
        Ok(values.into_any())
    }
}

impl PythonReferences for ArrayRead {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        self.element.traverse(visit)?;
        Ok(())
    }
}
