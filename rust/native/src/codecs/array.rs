//! A PostgreSQL array field: a list read element by element through the element field's codec, a
//! NULL element kept None.

use pyo3::prelude::*;
use pyo3::types::PyList;
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::options::Options;
use crate::codecs::read_codec::ReadCodec;
use crate::pg::result_cell::ResultCell;
use crate::pg::value::Value;
use crate::python::references::PythonReferences;

pub struct ArrayRead {
    element: Box<ReadCodec>,
    fallback: Py<PyAny>,
}

impl ArrayRead {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        let element_type = options.get_string("element_type")?;
        let element = ReadCodec::build(&element_type, &options.get_nested("element_options")?)?;
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

    /// The value of `cell`, a value of a `rust.native.pg` result - what `read()` gives for the list
    /// the driver would have returned, each element read straight from its decoded value.
    pub fn read_cell<'py>(&self, py: Python<'py>, cell: &ResultCell<'_>) -> PyResult<Bound<'py, PyAny>> {
        cell.with_value(|value| match value {
            Value::Null => Ok(py.None().into_bound(py)),
            Value::Array(items) => {
                let elements = items
                    .iter()
                    .map(|item| match item {
                        Value::Null => Ok(py.None().into_bound(py)),
                        _ => self.element.read_cell(py, &ResultCell::Value(item)),
                    })
                    .collect::<PyResult<Vec<_>>>()?;
                Ok(PyList::new(py, elements)?.into_any())
            }
            _ => self.read(value.into_pyobject(py)?),
        })?
    }
}

impl PythonReferences for ArrayRead {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        self.element.traverse(visit)?;
        Ok(())
    }
}
