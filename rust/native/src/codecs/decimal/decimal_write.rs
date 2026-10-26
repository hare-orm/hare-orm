use crate::codecs::inline_checks::InlineChecks;
use crate::codecs::options::Options;
use crate::python::objects;
use crate::python::references::PythonReferences;
use pyo3::intern;
use pyo3::types::{PyFloat, PyInt, PyString};
use pyo3::{PyTraverseError, PyVisit};

use super::Quantizer;
use pyo3::prelude::*;

pub struct DecimalWrite {
    quantizer: Quantizer,
    /// Every validator of the field checked inline - when `validate` is None.
    checks: InlineChecks,
    /// `field.validate` when a validator isn't inlined.
    validate: Option<Py<PyAny>>,
    null: bool,
    fallback: Py<PyAny>,
}

impl DecimalWrite {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(DecimalWrite {
            quantizer: Quantizer::from_options(options)?,
            checks: InlineChecks::from_options(options)?,
            validate: options.get_optional_object("validate")?,
            null: options.get_bool("null")?,
            fallback: options.get_object("fallback")?,
        })
    }

    pub fn write<'py>(
        &self,
        value: Bound<'py, PyAny>,
        instance: &Bound<'py, PyAny>,
        field_name: &Bound<'py, PyString>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() && self.null {
            return Ok(value);
        }
        let is_number = value.get_type().is(objects::decimal_type(py)?)
            || value.is_exact_instance_of::<PyInt>()
            || value.is_exact_instance_of::<PyFloat>();
        if !is_number {
            return self.fallback.bind(py).call1((value, instance));
        }
        let Ok(quantized) = self.quantizer.quantize(&value) else {
            return self.fallback.bind(py).call1((value, instance));
        };
        // NaN quantizes to itself - the field's validator refuses it.
        if !quantized.call_method0(intern!(py, "is_finite"))?.is_truthy()? {
            return self.fallback.bind(py).call1((value, instance));
        }
        if let Some(validate) = &self.validate {
            validate.bind(py).call1((&quantized,))?;
        } else {
            self.checks.check(&quantized, field_name)?;
        }
        Ok(quantized)
    }
}

impl PythonReferences for DecimalWrite {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        visit.call(&self.validate)?;
        self.quantizer.traverse(visit)?;
        self.checks.traverse(visit)?;
        Ok(())
    }
}
