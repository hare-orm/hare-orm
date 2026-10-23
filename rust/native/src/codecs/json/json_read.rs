use crate::codecs::options::Options;
use crate::python::ffi;
use crate::python::references::PythonReferences;
use pyo3::types::{PyBytes, PyString};
use pyo3::{PyTraverseError, PyVisit};

use super::has_long_digit_run;
use pyo3::prelude::*;

pub struct JsonRead {
    /// `field.decoder`.
    decoder: Py<PyAny>,
    /// `orjson.loads`, when the decoder is `JsonCodec.loads` and orjson is installed.
    fast_decoder: Option<Py<PyAny>>,
    /// `TypeAdapter.validate_python` of a declared `field_type`.
    validate_type: Option<Py<PyAny>>,
    fallback: Py<PyAny>,
}

impl JsonRead {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(JsonRead {
            decoder: options.get_object("decoder")?,
            fast_decoder: options.get_optional_object("fast_decoder")?,
            validate_type: options.get_optional_object("validate_type")?,
            fallback: options.get_object("fallback")?,
        })
    }

    fn get_decoder<'py>(&self, py: Python<'py>, text: &[u8]) -> &Bound<'py, PyAny> {
        match &self.fast_decoder {
            Some(fast_decoder) if !has_long_digit_run(text) => fast_decoder.bind(py),
            _ => self.decoder.bind(py),
        }
    }

    fn decode<'py>(&self, raw: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = raw.py();
        let data = if let Ok(text) = raw.cast::<PyString>() {
            match text.to_str() {
                Ok(text) => self.get_decoder(py, text.as_bytes()).call1((raw,))?,
                Err(_) => self.decoder.bind(py).call1((raw,))?,
            }
        } else if let Ok(bytes) = raw.cast::<PyBytes>() {
            self.get_decoder(py, bytes.as_bytes()).call1((raw,))?
        } else {
            raw.clone()
        };
        match &self.validate_type {
            Some(validate_type) if !data.is_none() => validate_type.bind(py).call1((data,)),
            _ => Ok(data),
        }
    }

    /// The value of a `json`/`jsonb` text `rust.native.pg` read, its UTF-8 bytes - what `read()`
    /// gives for the str the driver would have returned. orjson reads the bytes, with no str made
    /// in between; the field's own decoder still gets the str.
    pub fn read_text<'py>(&self, py: Python<'py>, text: &[u8]) -> PyResult<Bound<'py, PyAny>> {
        let Some(fast_decoder) = self.fast_decoder.as_ref().filter(|_| !has_long_digit_run(text)) else {
            return self.read(ffi::new_string_from_utf8(py, text)?.into_any());
        };
        let decoded =
            fast_decoder.bind(py).call1((PyBytes::new(py, text),)).and_then(|data| match &self.validate_type {
                Some(validate_type) if !data.is_none() => validate_type.bind(py).call1((data,)),
                _ => Ok(data),
            });
        match decoded {
            Ok(value) => Ok(value),
            // Invalid text or a value of another type - the field's own error.
            Err(_) => self.fallback.bind(py).call1((ffi::new_string_from_utf8(py, text)?,)),
        }
    }

    pub fn read<'py>(&self, raw: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        if raw.is_none() {
            return Ok(raw);
        }
        match self.decode(&raw) {
            Ok(value) => Ok(value),
            // Invalid text or a value of another type - the field's own error.
            Err(_) => self.fallback.bind(raw.py()).call1((raw,)),
        }
    }
}

impl PythonReferences for JsonRead {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.decoder)?;
        visit.call(&self.fast_decoder)?;
        visit.call(&self.validate_type)?;
        visit.call(&self.fallback)?;
        Ok(())
    }
}
