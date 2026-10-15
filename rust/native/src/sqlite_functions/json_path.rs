//! A JSON path holding a digit segment on SQLite - an object's key or an array's index, whichever the
//! value at that point is - giving what `json_extract()`/`json_type()` would. A scalar found here is
//! rendered here; an array or object found (written in its own key order) and anything this doesn't
//! read goes to the Python function.

use pyo3::prelude::*;
use pyo3::types::{PyFloat, PyInt, PyString};
use pyo3::{PyTraverseError, PyVisit};
use serde_json::Value;

use crate::sqlite_functions::json_canonical::write_json_string;

/// A JSON number as SQLite holds it - an integer past 64 bits as a REAL; None for one this doesn't
/// read.
fn get_sqlite_number<'py>(py: Python<'py>, number: &serde_json::Number) -> PyResult<Option<Bound<'py, PyAny>>> {
    let text = number.as_str();
    if text.bytes().any(|byte| matches!(byte, b'.' | b'e' | b'E')) {
        return match text.parse::<f64>() {
            Ok(float) => Ok(Some(PyFloat::new(py, float).into_any())),
            Err(_) => Ok(None),
        };
    }
    let Ok(integer) = text.parse::<i128>() else {
        return Ok(None);
    };
    Ok(Some(match i64::try_from(integer) {
        Ok(integer) => integer.into_pyobject(py)?.into_any(),
        #[expect(clippy::cast_precision_loss, reason = "float() of an int rounds the same way")]
        Err(_) => PyFloat::new(py, integer as f64).into_any(),
    }))
}

/// The `json_type()` name of a JSON value.
fn get_type_name(value: &Value) -> &'static str {
    match value {
        Value::Null => "null",
        Value::Bool(true) => "true",
        Value::Bool(false) => "false",
        Value::Number(number) => {
            if number.as_str().bytes().any(|byte| matches!(byte, b'.' | b'e' | b'E')) {
                "real"
            } else {
                "integer"
            }
        }
        Value::String(_) => "text",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
}

/// What a path leads to in a JSON value.
enum PathTarget<'a> {
    Found(&'a Value),
    Missing,
    /// A path part the Python function reads.
    LeftToPython,
}

/// The value at a path.
fn walk<'a>(mut value: &'a Value, parts: &[Value]) -> PathTarget<'a> {
    for part in parts {
        let next = match (part, value) {
            (Value::Number(number), Value::Array(elements)) if number.is_i64() => {
                let (Some(index), Ok(length)) = (number.as_i64(), i64::try_from(elements.len())) else {
                    return PathTarget::LeftToPython;
                };
                let index = if index >= 0 { index } else { length + index };
                match usize::try_from(index).ok().filter(|_| index < length) {
                    Some(index) => &elements[index],
                    None => return PathTarget::Missing,
                }
            }
            (Value::Number(number), Value::Object(members)) if number.is_i64() => {
                match number.as_i64().and_then(|key| members.get(&key.to_string())) {
                    Some(member) => member,
                    None => return PathTarget::Missing,
                }
            }
            (Value::String(key), Value::Object(members)) => match members.get(key) {
                Some(member) => member,
                None => return PathTarget::Missing,
            },
            (Value::Number(number), _) if number.is_i64() => return PathTarget::Missing,
            (Value::String(_), _) => return PathTarget::Missing,
            // A part of another type - Python decides.
            _ => return PathTarget::LeftToPython,
        };
        value = next;
    }
    PathTarget::Found(value)
}

/// The JSON path function; arguments it doesn't handle go to the Python one given.
#[pyclass(frozen, module = "rust.native.sqlite_functions")]
pub struct JsonPath {
    extract_in_python: Py<PyAny>,
}

#[pymethods]
impl JsonPath {
    #[new]
    fn new(extract_in_python: Py<PyAny>) -> Self {
        JsonPath { extract_in_python }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.extract_in_python)
    }

    /// The value at a path in the form `mode` names (`text`, `json` or `type`); None for a missing
    /// path or a NULL column.
    fn extract<'py>(
        &self,
        document: &Bound<'py, PyAny>,
        path_json: &Bound<'py, PyAny>,
        mode: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = document.py();
        if document.is_none() {
            return Ok(py.None().into_bound(py));
        }
        let parsed_document = if let Ok(text) = document.cast::<PyString>() {
            serde_json::from_str::<Value>(text.to_str()?).ok()
        } else if document.is_instance_of::<PyInt>() || document.is_instance_of::<PyFloat>() {
            serde_json::from_str::<Value>(document.str()?.to_str()?).ok()
        } else {
            None
        };
        let parts = path_json
            .cast::<PyString>()
            .ok()
            .and_then(|text| serde_json::from_str::<Vec<Value>>(text.to_str().ok()?).ok());
        let mode_text = mode.cast::<PyString>().ok().and_then(|text| text.to_str().ok());
        if let (Some(parsed_document), Some(parts), Some(mode_text)) = (parsed_document, parts, mode_text) {
            match walk(&parsed_document, &parts) {
                PathTarget::Missing => return Ok(py.None().into_bound(py)),
                PathTarget::Found(value) => {
                    if let Some(rendered) = Self::render(py, value, mode_text)? {
                        return Ok(rendered);
                    }
                }
                PathTarget::LeftToPython => {}
            }
        }
        self.extract_in_python.bind(py).call1((document, path_json, mode))
    }
}

impl JsonPath {
    /// A scalar in the form `mode` names; None for an array or object, or a number this doesn't read.
    fn render<'py>(py: Python<'py>, value: &Value, mode: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
        if mode == "type" {
            return Ok(Some(PyString::new(py, get_type_name(value)).into_any()));
        }
        Ok(Some(match value {
            Value::Bool(flag) if mode == "text" => i64::from(*flag).into_pyobject(py)?.into_any(),
            Value::Bool(flag) => PyString::new(py, if *flag { "true" } else { "false" }).into_any(),
            Value::Number(number) => return get_sqlite_number(py, number),
            Value::Null if mode == "text" => py.None().into_bound(py),
            Value::Null => PyString::new(py, "null").into_any(),
            Value::String(text) if mode == "text" => PyString::new(py, text).into_any(),
            Value::String(text) => {
                let mut out = String::new();
                write_json_string(text, &mut out);
                PyString::new(py, &out).into_any()
            }
            Value::Array(_) | Value::Object(_) => return Ok(None),
        }))
    }
}
