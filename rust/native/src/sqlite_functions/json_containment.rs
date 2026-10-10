//! Postgres jsonb containment (`@>`) and key existence (`?`, `?&`, `?|`) on SQLite, over JSON text
//! read as `json.loads()` reads it - a number compared as the int or float Python makes of it.

use pyo3::prelude::*;
use pyo3::types::{PyFloat, PyInt, PyString};
use pyo3::{PyTraverseError, PyVisit};
use serde_json::{Number, Value};

/// The largest magnitude up to which every integer is a double exactly.
const EXACT_DOUBLE_INTEGER_LIMIT: i128 = 1 << 53;

/// A JSON number as Python reads it.
enum PythonNumber {
    Int(i128),
    Float(f64),
}

impl PythonNumber {
    /// The int or float `json.loads()` makes of a number's text; None for an int past 128 bits.
    fn read(number: &Number) -> Option<Self> {
        let text = number.as_str();
        if text.bytes().any(|byte| matches!(byte, b'.' | b'e' | b'E')) {
            text.parse().ok().map(PythonNumber::Float)
        } else {
            text.parse().ok().map(PythonNumber::Int)
        }
    }

    /// Whether two numbers are equal as Python compares them; None when this can't tell exactly.
    #[expect(clippy::float_cmp, reason = "Python compares the numbers exactly")]
    #[expect(clippy::cast_precision_loss, reason = "only an integer a double holds exactly is converted")]
    fn equals(&self, other: &PythonNumber) -> Option<bool> {
        match (self, other) {
            (PythonNumber::Int(left), PythonNumber::Int(right)) => Some(left == right),
            (PythonNumber::Float(left), PythonNumber::Float(right)) => Some(left == right),
            (PythonNumber::Int(integer), PythonNumber::Float(float))
            | (PythonNumber::Float(float), PythonNumber::Int(integer)) => {
                (integer.abs() <= EXACT_DOUBLE_INTEGER_LIMIT).then_some(*integer as f64 == *float)
            }
        }
    }
}

/// Whether two JSON scalars are the same value (`1` equals `1.0`, never `true`); None when this
/// can't tell exactly.
fn is_same_scalar(container: &Value, wanted: &Value) -> Option<bool> {
    Some(match (container, wanted) {
        (Value::Number(left), Value::Number(right)) => {
            return PythonNumber::read(left)?.equals(&PythonNumber::read(right)?);
        }
        (Value::Bool(left), Value::Bool(right)) => left == right,
        (Value::String(left), Value::String(right)) => left == right,
        (Value::Null, Value::Null) => true,
        _ => false,
    })
}

/// Whether `container` contains `wanted` - objects by key, arrays by element (order and repeats
/// ignored), scalars by equality; at the top level an array also contains a scalar element. None
/// when this can't tell exactly.
fn contains_value(container: &Value, wanted: &Value, top_level: bool) -> Option<bool> {
    match container {
        Value::Object(members) => {
            let Value::Object(wanted_members) = wanted else {
                return Some(false);
            };
            for (key, item) in wanted_members {
                let Some(member) = members.get(key) else {
                    return Some(false);
                };
                if !contains_value(member, item, false)? {
                    return Some(false);
                }
            }
            Some(true)
        }
        Value::Array(elements) => match wanted {
            Value::Array(items) => {
                for item in items {
                    let mut found = false;
                    for element in elements {
                        if contains_value(element, item, false)? {
                            found = true;
                            break;
                        }
                    }
                    if !found {
                        return Some(false);
                    }
                }
                Some(true)
            }
            Value::Object(_) => Some(false),
            _ if !top_level => Some(false),
            _ => {
                for element in elements {
                    if !element.is_array() && !element.is_object() && is_same_scalar(element, wanted)? {
                        return Some(true);
                    }
                }
                Some(false)
            }
        },
        _ => {
            if wanted.is_array() || wanted.is_object() {
                return Some(false);
            }
            is_same_scalar(container, wanted)
        }
    }
}

/// Whether a JSON value holds `key` the way jsonb `?` tests it.
fn contains_key(value: &Value, key: &str) -> bool {
    match value {
        Value::Object(members) => members.contains_key(key),
        Value::Array(items) => items.iter().any(|item| item.as_str() == Some(key)),
        Value::String(text) => text == key,
        _ => false,
    }
}

/// The JSON value of a function argument - JSON text, or a number a path value stays as; None for
/// anything this reads otherwise than `json.loads()`.
fn read_document(document: &Bound<'_, PyAny>) -> PyResult<Option<Value>> {
    if let Ok(text) = document.cast::<PyString>() {
        return Ok(serde_json::from_str(text.to_str()?).ok());
    }
    if document.is_instance_of::<PyInt>() {
        return Ok(serde_json::from_str(document.str()?.to_str()?).ok());
    }
    if let Ok(number) = document.cast::<PyFloat>() {
        let number = number.value();
        if !number.is_finite() {
            return Ok(None);
        }
        let mut buffer = ryu::Buffer::new();
        return Ok(serde_json::from_str(buffer.format_finite(number)).ok());
    }
    Ok(None)
}

/// The jsonb containment and key functions; an argument they don't read themselves goes to the
/// Python ones given.
#[pyclass(frozen, module = "rust.native.sqlite_functions")]
pub struct JsonContainment {
    // The Python tests, for an argument these functions do not read.
    contains: Py<PyAny>,
    has_key: Py<PyAny>,
    has_keys: Py<PyAny>,
}

#[pymethods]
impl JsonContainment {
    #[new]
    fn new(contains: Py<PyAny>, has_key: Py<PyAny>, has_keys: Py<PyAny>) -> Self {
        JsonContainment { contains, has_key, has_keys }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.contains)?;
        visit.call(&self.has_key)?;
        visit.call(&self.has_keys)
    }

    /// 1 when `container` contains `wanted`, 0 otherwise, None when either is NULL.
    fn contains(&self, container: &Bound<'_, PyAny>, wanted: &Bound<'_, PyAny>) -> PyResult<Option<i32>> {
        if container.is_none() || wanted.is_none() {
            return Ok(None);
        }
        if let (Some(container_value), Some(wanted_value)) = (read_document(container)?, read_document(wanted)?) {
            if let Some(result) = contains_value(&container_value, &wanted_value, true) {
                return Ok(Some(i32::from(result)));
            }
        }
        self.contains.bind(container.py()).call1((container, wanted))?.extract()
    }

    /// 1 when the document holds `key`, 0 otherwise, None for a NULL document or key.
    fn has_key(&self, document: &Bound<'_, PyAny>, key: &Bound<'_, PyAny>) -> PyResult<Option<i32>> {
        if document.is_none() || key.is_none() {
            return Ok(None);
        }
        if let (Some(value), Ok(key_text)) = (read_document(document)?, key.cast::<PyString>()) {
            return Ok(Some(i32::from(contains_key(&value, key_text.to_str()?))));
        }
        self.has_key.bind(document.py()).call1((document, key))?.extract()
    }

    /// 1 when the document holds all (`mode` `all`) or any of the keys of a JSON array, 0
    /// otherwise, None for a NULL document.
    fn has_keys(
        &self,
        document: &Bound<'_, PyAny>,
        keys_json: &Bound<'_, PyAny>,
        mode: &Bound<'_, PyAny>,
    ) -> PyResult<Option<i32>> {
        if document.is_none() {
            return Ok(None);
        }
        let keys = keys_json
            .cast::<PyString>()
            .ok()
            .and_then(|text| serde_json::from_str::<Vec<String>>(text.to_str().ok()?).ok());
        let mode_text = mode.cast::<PyString>().ok().and_then(|text| text.to_str().ok());
        if let (Some(value), Some(keys), Some(mode_text)) = (read_document(document)?, keys, mode_text) {
            let mut matches = keys.iter().map(|key| contains_key(&value, key));
            let result =
                if mode_text == "all" { matches.all(|matched| matched) } else { matches.any(|matched| matched) };
            return Ok(Some(i32::from(result)));
        }
        self.has_keys.bind(document.py()).call1((document, keys_json, mode))?.extract()
    }
}
