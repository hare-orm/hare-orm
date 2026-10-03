//! Builds the bound values of model instances - one row per instance, a value per column.

use pyo3::prelude::*;
use pyo3::types::PyList;
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::field_codec::FieldCodec;
use crate::python::ffi;

/// The written columns of one model, and how each is written.
#[pyclass(frozen, module = "rust.native.rows")]
pub struct ModelWriter {
    codecs: Vec<Py<FieldCodec>>,
}

impl ModelWriter {
    fn write_instance<'py>(&self, instance: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyList>> {
        let py = instance.py();
        let mut values = ffi::ListBuilder::new(py, self.codecs.len())?;
        for (index, codec) in self.codecs.iter().enumerate() {
            let codec = codec.get();
            let value = instance.getattr(codec.name.bind(py))?;
            values.set(index, codec.write_value(value, instance)?);
        }
        Ok(values.finish())
    }
}

#[pymethods]
impl ModelWriter {
    /// A writer of `codecs`, one per written column in column order.
    #[new]
    fn new(codecs: Vec<Py<FieldCodec>>) -> Self {
        ModelWriter { codecs }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        for codec in &self.codecs {
            visit.call(codec)?;
        }
        Ok(())
    }

    /// The row of bound values of each instance.
    fn write_rows<'py>(&self, instances: &Bound<'py, PyList>) -> PyResult<Bound<'py, PyList>> {
        let py = instances.py();
        let mut rows = ffi::ListBuilder::new(py, instances.len())?;
        for (index, instance) in instances.iter().enumerate() {
            rows.set(index, self.write_instance(&instance)?.into_any());
        }
        Ok(rows.finish())
    }

    /// The bound values of one instance.
    fn write_row<'py>(&self, instance: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyList>> {
        self.write_instance(instance)
    }
}
