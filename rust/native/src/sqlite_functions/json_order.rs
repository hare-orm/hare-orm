//! The order of JSON values on SQLite, as Postgres orders `jsonb`: object > array > boolean > number >
//! string > null, a container with more members greater, an object's keys taken shortest first then
//! by bytes; at the top level an empty array sorts below null and a scalar below an array of one
//! element - the comparison function and the sort key.

use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyo3::{PyTraverseError, PyVisit};
use serde_json::Value;

use crate::sqlite_functions::decimal_number::DecimalNumber;
use crate::sqlite_functions::sqlite_value_key::{write_terminated_text, SqliteValue};

/// Writes the key of a JSON value below the top level; false for a number this reads no key of.
fn write_value_key(value: &Value, key: &mut Vec<u8>) -> bool {
    match value {
        Value::Null => key.push(0x00),
        Value::String(text) => {
            key.push(0x01);
            write_terminated_text(text, key);
        }
        Value::Number(number) => {
            let Some(number) = DecimalNumber::parse(number.as_str()) else {
                return false;
            };
            key.push(0x02);
            number.write_key(key);
        }
        Value::Bool(flag) => key.extend_from_slice(&[0x03, u8::from(*flag)]),
        Value::Array(elements) => {
            key.push(0x04);
            key.extend_from_slice(&(elements.len() as u64).to_be_bytes());
            for element in elements {
                if !write_value_key(element, key) {
                    return false;
                }
            }
        }
        Value::Object(members) => {
            key.push(0x05);
            key.extend_from_slice(&(members.len() as u64).to_be_bytes());
            let mut names: Vec<&String> = members.keys().collect();
            names.sort_by(|left, right| {
                left.len().cmp(&right.len()).then_with(|| left.as_bytes().cmp(right.as_bytes()))
            });
            for name in names {
                key.push(0x01);
                write_terminated_text(name, key);
                if !write_value_key(&members[name.as_str()], key) {
                    return false;
                }
            }
        }
    }
    true
}

/// The key of a top-level JSON value: an object above everything else; anything else by its size
/// (a scalar counting as one element), then an array above a scalar, then its own key.
fn get_top_level_key(value: &Value) -> Option<Vec<u8>> {
    let mut key = Vec::new();
    if let Value::Object(_) = value {
        key.push(0x02);
    } else {
        let size = if let Value::Array(elements) = value { elements.len() } else { 1 };
        key.push(0x01);
        key.extend_from_slice(&(size as u64).to_be_bytes());
        key.push(u8::from(value.is_array()));
    }
    write_value_key(value, &mut key).then_some(key)
}

/// The key of a SQLite value read as JSON.
enum JsonKey {
    Null,
    Key(Vec<u8>),
    /// A value the Python key reads - a number this reads no key of, text serde reads otherwise than
    /// `json.loads()`.
    Unread,
}

/// The key of a SQLite value - a number as itself, text or a BLOB as JSON text.
fn get_key(value: &Bound<'_, PyAny>) -> PyResult<JsonKey> {
    let json_value = match SqliteValue::read(value)? {
        Some(SqliteValue::Null) => return Ok(JsonKey::Null),
        Some(SqliteValue::Number(number)) => {
            // A top-level scalar: one element, not an array, then the number.
            let mut key = vec![0x01];
            key.extend_from_slice(&1_u64.to_be_bytes());
            key.extend_from_slice(&[0x00, 0x02]);
            number.write_key(&mut key);
            return Ok(JsonKey::Key(key));
        }
        Some(SqliteValue::Text(text)) => serde_json::from_str::<Value>(text).ok(),
        Some(SqliteValue::Blob(blob)) => serde_json::from_slice::<Value>(blob).ok(),
        None => None,
    };
    Ok(json_value.and_then(|json_value| get_top_level_key(&json_value)).map_or(JsonKey::Unread, JsonKey::Key))
}

/// The jsonb order's functions; a value they don't read themselves goes to the Python ones given.
#[pyclass(frozen, module = "rust.native.sqlite_functions")]
pub struct JsonOrder {
    /// `(value) -> bytes | None`, the key of any value.
    fallback_sort_key: Py<PyAny>,
    /// `(left, right) -> int | None`, the comparison of two values.
    fallback_compare: Py<PyAny>,
}

#[pymethods]
impl JsonOrder {
    #[new]
    fn new(fallback_sort_key: Py<PyAny>, fallback_compare: Py<PyAny>) -> Self {
        JsonOrder { fallback_sort_key, fallback_compare }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback_sort_key)?;
        visit.call(&self.fallback_compare)
    }

    /// The byte key `ORDER BY` sorts a JSON value by, as `compare()` orders values.
    fn sort_key<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        match get_key(value)? {
            JsonKey::Key(key) => Ok(PyBytes::new(py, &key).into_any()),
            JsonKey::Null => Ok(py.None().into_bound(py)),
            JsonKey::Unread => self.fallback_sort_key.bind(py).call1((value,)),
        }
    }

    /// -1, 0 or 1 as `left` sorts before, with or after `right`; None when either is NULL.
    fn compare<'py>(&self, left: &Bound<'py, PyAny>, right: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = left.py();
        match (get_key(left)?, get_key(right)?) {
            (JsonKey::Key(left_key), JsonKey::Key(right_key)) => {
                Ok((left_key.cmp(&right_key) as i32).into_pyobject(py)?.into_any())
            }
            (JsonKey::Null, _) | (_, JsonKey::Null) => Ok(py.None().into_bound(py)),
            _ => self.fallback_compare.bind(py).call1((left, right)),
        }
    }
}
