//! A `DecimalField`: quantized to its scale with the field's context, a zero without its sign -
//! `DecimalField._quantize()` through the same `Decimal` methods, without its Python frame.

use crate::codecs::options::Options;
use crate::python::objects;
use crate::python::references::PythonReferences;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::{PyTraverseError, PyVisit};

mod decimal_read;
mod decimal_write;

pub use decimal_read::DecimalRead;
pub use decimal_write::DecimalWrite;

/// The quantizing both directions share.
struct Quantizer {
    /// `field.quant`.
    quant: Py<PyAny>,
    /// `field.quantize_context`.
    context: Py<PyAny>,
}

impl Quantizer {
    fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(Quantizer { quant: options.get_object("quant")?, context: options.get_object("context")? })
    }

    /// `value` quantized; an error for a value the field reports itself.
    fn quantize<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        let decimal_type = objects::decimal_type(py)?;
        let decimal = if value.get_type().is(decimal_type) { value.clone() } else { decimal_type.call1((value,))? };
        decimal.call_method1(intern!(py, "quantize"), (self.quant.bind(py), py.None(), self.context.bind(py)))
    }
}

impl PythonReferences for Quantizer {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.quant)?;
        visit.call(&self.context)?;
        Ok(())
    }
}
