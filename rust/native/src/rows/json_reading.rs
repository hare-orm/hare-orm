//! JSON decoding of hare: orjson, or the direct UTF-8 parser below - which builds Python containers and
//! preserves exact integers - for a text where an integer past orjson's range may be.

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString};

const JSON_MAXIMUM_NESTING: usize = 1024;
/// The shortest run of digits that may be an integer orjson can't decode exactly - past 64 bits.
const LONG_INTEGER_MIN_DIGITS: usize = 19;
/// `orjson.loads`, imported on the first decode.
static ORJSON_LOADS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

struct JsonReader<'a, 'py> {
    py: Python<'py>,
    bytes: &'a [u8],
    index: usize,
}

impl<'py> JsonReader<'_, 'py> {
    #[inline]
    fn skip_whitespace(&mut self) {
        while self.index < self.bytes.len() {
            match self.bytes[self.index] {
                b' ' | b'\t' | b'\n' | b'\r' => self.index += 1,
                _ => break,
            }
        }
    }

    fn read_value(&mut self, depth: usize) -> PyResult<Bound<'py, PyAny>> {
        self.skip_whitespace();
        match self.bytes.get(self.index) {
            Some(b'{') => self.read_object(depth),
            Some(b'[') => self.read_array(depth),
            Some(b'"') => Ok(self.read_string()?.into_any()),
            Some(b'-' | b'0'..=b'9') => self.read_number(),
            Some(b't') if self.bytes[self.index..].starts_with(b"true") => {
                self.index += 4;
                Ok(PyBool::new(self.py, true).to_owned().into_any())
            }
            Some(b'f') if self.bytes[self.index..].starts_with(b"false") => {
                self.index += 5;
                Ok(PyBool::new(self.py, false).to_owned().into_any())
            }
            Some(b'n') if self.bytes[self.index..].starts_with(b"null") => {
                self.index += 4;
                Ok(self.py.None().into_bound(self.py))
            }
            _ => Err(PyValueError::new_err("invalid JSON value")),
        }
    }

    fn read_object(&mut self, depth: usize) -> PyResult<Bound<'py, PyAny>> {
        if depth >= JSON_MAXIMUM_NESTING {
            return Err(PyValueError::new_err("JSON nesting exceeds 1024 containers"));
        }
        let next_depth = depth + 1;
        self.index += 1;
        self.skip_whitespace();
        let result = PyDict::new(self.py);
        if self.bytes.get(self.index) == Some(&b'}') {
            self.index += 1;
            return Ok(result.into_any());
        }
        loop {
            if self.bytes.get(self.index) != Some(&b'"') {
                return Err(PyValueError::new_err("expected string key in JSON object"));
            }
            let key = self.read_string()?;
            self.skip_whitespace();
            if self.bytes.get(self.index) != Some(&b':') {
                return Err(PyValueError::new_err("expected ':' after key in JSON object"));
            }
            self.index += 1;
            let value = self.read_value(next_depth)?;
            // SAFETY: `result`, `key`, and `value` are valid owned Python objects under the GIL.
            if unsafe { ffi::PyDict_SetItem(result.as_ptr(), key.as_ptr(), value.as_ptr()) } != 0 {
                return Err(PyErr::fetch(self.py));
            }
            self.skip_whitespace();
            match self.bytes.get(self.index) {
                Some(b',') => {
                    self.index += 1;
                    self.skip_whitespace();
                }
                Some(b'}') => {
                    self.index += 1;
                    return Ok(result.into_any());
                }
                _ => return Err(PyValueError::new_err("expected ',' or '}' in JSON object")),
            }
        }
    }

    fn read_array(&mut self, depth: usize) -> PyResult<Bound<'py, PyAny>> {
        if depth >= JSON_MAXIMUM_NESTING {
            return Err(PyValueError::new_err("JSON nesting exceeds 1024 containers"));
        }
        let next_depth = depth + 1;
        self.index += 1;
        self.skip_whitespace();
        let result = PyList::empty(self.py);
        if self.bytes.get(self.index) == Some(&b']') {
            self.index += 1;
            return Ok(result.into_any());
        }
        loop {
            let value = self.read_value(next_depth)?;
            // SAFETY: `result` and `value` are valid owned Python objects under the GIL.
            if unsafe { ffi::PyList_Append(result.as_ptr(), value.as_ptr()) } != 0 {
                return Err(PyErr::fetch(self.py));
            }
            self.skip_whitespace();
            match self.bytes.get(self.index) {
                Some(b',') => self.index += 1,
                Some(b']') => {
                    self.index += 1;
                    return Ok(result.into_any());
                }
                _ => return Err(PyValueError::new_err("expected ',' or ']' in JSON array")),
            }
        }
    }

    #[inline]
    fn read_string(&mut self) -> PyResult<Bound<'py, PyString>> {
        let quote_start = self.index;
        self.index += 1;
        let content_start = self.index;
        while self.index < self.bytes.len() {
            let byte = self.bytes[self.index];
            if byte == b'"' {
                // SAFETY: `self.bytes` is validated UTF-8 and ASCII `"` is a valid UTF-8 boundary.
                let slice = unsafe { std::str::from_utf8_unchecked(&self.bytes[content_start..self.index]) };
                self.index += 1;
                return Ok(PyString::new(self.py, slice));
            }
            if byte == b'\\' {
                let mut stream = serde_json::Deserializer::from_slice(&self.bytes[quote_start..]).into_iter::<String>();
                let decoded = stream
                    .next()
                    .ok_or_else(|| PyValueError::new_err("unterminated JSON string"))?
                    .map_err(|error| PyValueError::new_err(error.to_string()))?;
                self.index = quote_start + stream.byte_offset();
                return Ok(PyString::new(self.py, &decoded));
            }
            if byte < 0x20 {
                return Err(PyValueError::new_err("control character in JSON string"));
            }
            self.index += 1;
        }
        Err(PyValueError::new_err("unterminated JSON string"))
    }

    #[inline]
    fn read_number(&mut self) -> PyResult<Bound<'py, PyAny>> {
        let start = self.index;
        let negative = self.bytes[self.index] == b'-';
        if negative {
            self.index += 1;
        }
        let mut accumulator: u64 = 0;
        let mut digit_count: usize = 0;
        match self.bytes.get(self.index) {
            Some(b'0') => {
                self.index += 1;
                digit_count = 1;
                if matches!(self.bytes.get(self.index), Some(b'0'..=b'9')) {
                    return Err(PyValueError::new_err("leading zero in JSON number"));
                }
            }
            Some(b'1'..=b'9') => {
                while self.index < self.bytes.len() && self.bytes[self.index].is_ascii_digit() {
                    if digit_count < 18 {
                        accumulator = accumulator * 10 + u64::from(self.bytes[self.index] - b'0');
                    }
                    digit_count += 1;
                    self.index += 1;
                }
            }
            _ => return Err(PyValueError::new_err("invalid JSON number")),
        }

        let mut is_float = false;
        if self.bytes.get(self.index) == Some(&b'.') {
            is_float = true;
            self.index += 1;
            if !matches!(self.bytes.get(self.index), Some(b'0'..=b'9')) {
                return Err(PyValueError::new_err("missing fraction digits in JSON number"));
            }
            while self.index < self.bytes.len() && self.bytes[self.index].is_ascii_digit() {
                self.index += 1;
            }
        }
        if matches!(self.bytes.get(self.index), Some(b'e' | b'E')) {
            is_float = true;
            self.index += 1;
            if matches!(self.bytes.get(self.index), Some(b'+' | b'-')) {
                self.index += 1;
            }
            if !matches!(self.bytes.get(self.index), Some(b'0'..=b'9')) {
                return Err(PyValueError::new_err("missing exponent digits in JSON number"));
            }
            while self.index < self.bytes.len() && self.bytes[self.index].is_ascii_digit() {
                self.index += 1;
            }
        }

        if !is_float {
            if digit_count <= 18 {
                if negative {
                    let signed = -(accumulator as i64);
                    return Ok(signed.into_pyobject(self.py).unwrap().into_any());
                }
                return Ok(accumulator.into_pyobject(self.py).unwrap().into_any());
            }
            // SAFETY: the number token consists exclusively of ASCII bytes.
            let number = unsafe { std::str::from_utf8_unchecked(&self.bytes[start..self.index]) };
            if let Ok(integer) = number.parse::<i64>() {
                return Ok(integer.into_pyobject(self.py).unwrap().into_any());
            }
            if let Ok(integer) = number.parse::<u64>() {
                return Ok(integer.into_pyobject(self.py).unwrap().into_any());
            }
            // Python's integer conversion preserves its configured decimal digit guard.
            return self.py.get_type::<PyInt>().call1((number,));
        }

        // SAFETY: the number token consists exclusively of ASCII bytes.
        let number = unsafe { std::str::from_utf8_unchecked(&self.bytes[start..self.index]) };
        let parsed = number.parse::<f64>().map_err(|error| PyValueError::new_err(error.to_string()))?;
        if !parsed.is_finite() {
            return Err(PyValueError::new_err("JSON number is not finite"));
        }
        Ok(PyFloat::new(self.py, parsed).into_any())
    }
}

/// The UTF-8 bytes of a JSON text given as `str` or `bytes`.
fn get_text_bytes<'a>(text: &'a Bound<'_, PyAny>) -> PyResult<&'a [u8]> {
    if let Ok(text) = text.cast::<PyString>() {
        Ok(text.to_str().map_err(|error| PyValueError::new_err(error.to_string()))?.as_bytes())
    } else if let Ok(text) = text.cast::<PyBytes>() {
        Ok(std::str::from_utf8(text.as_bytes()).map_err(|error| PyValueError::new_err(error.to_string()))?.as_bytes())
    } else {
        Err(PyTypeError::new_err("JSON input must be str or bytes"))
    }
}

/// Whether a JSON text has a run of `LONG_INTEGER_MIN_DIGITS` ASCII digits - where an integer orjson
/// can't decode exactly may be. A run inside a string counts too.
fn has_long_digit_run(bytes: &[u8]) -> bool {
    let mut run = 0;
    for byte in bytes {
        if byte.is_ascii_digit() {
            run += 1;
            if run >= LONG_INTEGER_MIN_DIGITS {
                return true;
            }
        } else {
            run = 0;
        }
    }
    false
}

/// Decodes UTF-8 JSON text with the direct parser, reading an integer of any size exactly.
fn read_exact<'py>(py: Python<'py>, bytes: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let mut reader = JsonReader { py, bytes, index: 0 };
    let value = reader.read_value(0)?;
    reader.skip_whitespace();
    if reader.index != bytes.len() {
        return Err(PyValueError::new_err("trailing characters in JSON input"));
    }
    Ok(value)
}

/// Decodes JSON text (`str` or `bytes`) - `JsonCodec.loads`: with orjson, unless a run of
/// `LONG_INTEGER_MIN_DIGITS` digits may be an integer past its range, which the direct parser reads
/// exactly. NaN and Infinity aren't JSON and are refused; an invalid text raises `ValueError`.
#[pyfunction]
pub fn loads_json<'py>(text: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = text.py();
    let bytes = get_text_bytes(text)?;
    if has_long_digit_run(bytes) {
        return read_exact(py, bytes);
    }
    let loads = ORJSON_LOADS.get_or_try_init(py, || py.import("orjson")?.getattr("loads").map(Bound::unbind))?;
    loads.bind(py).call1((text,))
}
