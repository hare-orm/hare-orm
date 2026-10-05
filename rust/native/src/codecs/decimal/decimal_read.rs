use crate::codecs::options::Options;
use crate::python::objects;
use crate::python::references::PythonReferences;
use pyo3::{PyTraverseError, PyVisit};

use super::Quantizer;
use pyo3::prelude::*;

pub struct DecimalRead {
    quantizer: Quantizer,
    /// `field.decimal_places` - None when not given, and text is then always quantized.
    decimal_places: Option<usize>,
    /// The precision of `field.quantize_context`.
    precision: Option<usize>,
    fallback: Py<PyAny>,
}

impl DecimalRead {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        let get_count = |name: &str| -> PyResult<Option<usize>> {
            Ok(options.get_optional_integer(name)?.and_then(|count| usize::try_from(count).ok()))
        };
        Ok(DecimalRead {
            quantizer: Quantizer::from_options(options)?,
            decimal_places: get_count("decimal_places")?,
            precision: get_count("precision")?,
            fallback: options.get_object("fallback")?,
        })
    }

    /// Whether `text`, a number's positional text, is already what quantizing gives: exactly
    /// `decimal_places` digits after the point, no more significant digits than the precision.
    fn is_quantized_text(&self, text: &str) -> bool {
        let (Some(decimal_places), Some(precision)) = (self.decimal_places, self.precision) else {
            return false;
        };
        let unsigned = text.strip_prefix('-').unwrap_or(text);
        let (whole, fraction) = unsigned.split_once('.').unwrap_or((unsigned, ""));
        if fraction.len() != decimal_places
            || whole.is_empty()
            || !whole.bytes().chain(fraction.bytes()).all(|byte| byte.is_ascii_digit())
        {
            return false;
        }
        let significant_whole = whole.trim_start_matches('0');
        let significant_digits = if significant_whole.is_empty() {
            fraction.trim_start_matches('0').len()
        } else {
            significant_whole.len() + fraction.len()
        };
        significant_digits <= precision
    }

    /// The value of a number's positional text `rust.native.pg` read - what `read()` gives for its
    /// `Decimal`, quantized only when the text isn't already.
    pub fn read_text<'py>(&self, py: Python<'py>, text: &str) -> PyResult<Bound<'py, PyAny>> {
        let decimal = objects::decimal_type(py)?.call1((text,))?;
        if self.is_quantized_text(text) {
            return Ok(decimal);
        }
        self.read(decimal)
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

impl PythonReferences for DecimalRead {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        self.quantizer.traverse(visit)?;
        Ok(())
    }
}
