//! `check_json_storable()` - the walk `JSONField.check_storable_value()` makes over a decoded JSON
//! value before writing it, done natively: every node of a nested payload was an `isinstance`
//! chain in Python, the bulk of a JSON write's cost.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

/// The value holds nothing a JSON column can't store, and no int outside orjson's range.
const STORABLE: u8 = 0;
/// The value holds an int outside the range orjson encodes (below -2**63 or above 2**64 - 1).
const STORABLE_WITH_LONG_INTEGER: u8 = 1;
/// A str value or dict key holds a null byte.
const NULL_BYTE: u8 = 2;
/// A float is NaN or infinite - returned with the float itself, for the error message.
const NON_FINITE_FLOAT: u8 = 3;

/// Walks `value` exactly as `JSONField.check_storable_value()` does - a stack popped from the
/// end, a dict's keys pushed before its values - so the first problem found is the same one.
///
/// Returns `(code, float)`: `code` is one of the constants above, `float` the offending value
/// for `NON_FINITE_FLOAT` and None otherwise.
#[pyfunction]
pub fn check_json_storable<'py>(py: Python<'py>, value: Bound<'py, PyAny>) -> PyResult<(u8, Bound<'py, PyAny>)> {
    let mut has_long_integer = false;
    let mut pending: Vec<Bound<'py, PyAny>> = vec![value];
    while let Some(item) = pending.pop() {
        if item.is_instance_of::<PyString>() {
            if item.contains("\u{0}")? {
                return Ok((NULL_BYTE, py.None().into_bound(py)));
            }
        } else if let Ok(dict) = item.cast::<PyDict>() {
            for key in dict.keys() {
                pending.push(key);
            }
            for dict_value in dict.values() {
                pending.push(dict_value);
            }
        } else if let Ok(list) = item.cast::<PyList>() {
            for element in list.iter() {
                pending.push(element);
            }
        } else if let Ok(tuple) = item.cast::<PyTuple>() {
            for element in tuple.iter() {
                pending.push(element);
            }
        } else if item.is_instance_of::<PyFloat>() {
            if !item.extract::<f64>()?.is_finite() {
                return Ok((NON_FINITE_FLOAT, item));
            }
        } else if item.is_instance_of::<PyInt>() && item.extract::<i64>().is_err() && item.extract::<u64>().is_err() {
            has_long_integer = true;
        }
    }
    let code = if has_long_integer { STORABLE_WITH_LONG_INTEGER } else { STORABLE };
    Ok((code, py.None().into_bound(py)))
}
