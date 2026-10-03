//! A `DecimalField`: quantized to its scale with the field's context, a zero without its sign -
//! `DecimalField._quantize()` through the same `Decimal` methods, without its Python frame.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyFloat, PyInt, PyString};
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::inline_checks::InlineChecks;
use crate::codecs::options::Options;
use crate::python::objects;
use crate::python::references::PythonReferences;

/// `SQLITE_DECIMAL_FIXED_POINT_MAX_EXPONENT` - past it SQLite gets scientific text.
const FIXED_POINT_MAX_EXPONENT: i64 = 1000;

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

    /// `value` quantized, a zero made positive; an error for a value the field reports itself.
    fn quantize<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        let decimal_type = objects::decimal_type(py)?;
        let decimal = if value.get_type().is(decimal_type) { value.clone() } else { decimal_type.call1((value,))? };
        let quantized =
            decimal.call_method1(intern!(py, "quantize"), (self.quant.bind(py), py.None(), self.context.bind(py)))?;
        if quantized.is_truthy()? {
            Ok(quantized)
        } else {
            quantized.call_method0(intern!(py, "copy_abs"))
        }
    }
}

pub struct DecimalRead {
    quantizer: Quantizer,
    fallback: Py<PyAny>,
}

impl DecimalRead {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(DecimalRead { quantizer: Quantizer::from_options(options)?, fallback: options.get_object("fallback")? })
    }

    pub fn read<'py>(&self, raw: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        if raw.is_none() {
            return Ok(raw);
        }
        match self.quantizer.quantize(&raw) {
            Ok(value) => Ok(value),
            // Not a number, or too long for the context - the field's own error.
            Err(_) => self.fallback.bind(raw.py()).call1((raw,)),
        }
    }
}

pub struct DecimalWrite {
    quantizer: Quantizer,

    decimal_places: i64,
    /// Every validator of the field checked inline - when `validate` is None.
    checks: InlineChecks,
    /// `field.validate` when a validator isn't inlined.
    validate: Option<Py<PyAny>>,
    /// Bound as SQLite's fixed-point text rather than a `Decimal`.
    text: bool,
    null: bool,
    fallback: Py<PyAny>,
}

impl DecimalWrite {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(DecimalWrite {
            quantizer: Quantizer::from_options(options)?,

            decimal_places: options.get_integer("decimal_places")?,
            checks: InlineChecks::from_options(options)?,
            validate: options.get_optional_object("validate")?,
            text: options.get_bool("text")?,
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
        if !self.text {
            return Ok(quantized);
        }
        let adjusted: i64 = quantized.call_method0(intern!(py, "adjusted"))?.extract()?;
        if adjusted <= FIXED_POINT_MAX_EXPONENT && self.decimal_places <= FIXED_POINT_MAX_EXPONENT {
            quantized.call_method1(intern!(py, "__format__"), ("f",))
        } else {
            Ok(quantized.str()?.into_any())
        }
    }
}

impl PythonReferences for Quantizer {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.quant)?;
        visit.call(&self.context)?;
        Ok(())
    }
}

impl PythonReferences for DecimalRead {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        self.quantizer.traverse(visit)?;
        Ok(())
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
