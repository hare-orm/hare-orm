//! `check_json_storable()` - the walk `JSONField.check_storable_value()` makes over a decoded JSON
//! value before writing it, done natively: every node of a nested payload was an `isinstance`
//! chain in Python, the bulk of a JSON write's cost.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyInt, PyList, PyTuple};

use crate::codecs::json::MAXIMUM_DEPTH;

/// The value holds nothing a JSON column can't store, and no int outside orjson's range.
const STORABLE: u8 = 0;
/// The value holds an int outside the range orjson encodes (below -2**63 or above 2**64 - 1).
const STORABLE_WITH_LONG_INTEGER: u8 = 1;
/// A float is NaN or infinite - returned with the float itself, for the error message.
const NON_FINITE_FLOAT: u8 = 3;
/// A list, tuple or dict holds itself - the walk would never end.
const CIRCULAR_REFERENCE: u8 = 4;
/// Lists, tuples and dicts nest more than `MAXIMUM_DEPTH` deep.
const NESTED_TOO_DEEPLY: u8 = 5;
/// The depth of the containers' path from which each container is looked for on it - a value
/// holding itself nests without end, so it gets there; a shallower one pays no search.
const CIRCULAR_REFERENCE_CHECK_DEPTH: usize = 32;

/// Walks `value` exactly as `JSONField.check_storable_value()` does - a stack popped from the
/// end, a dict's keys pushed before its values - so the first problem found is the same one.
///
/// Returns `(code, float)`: `code` is one of the constants above, `float` the offending value
/// for `NON_FINITE_FLOAT` and None otherwise.
#[pyfunction]
pub fn check_json_storable<'py>(py: Python<'py>, value: Bound<'py, PyAny>) -> PyResult<(u8, Bound<'py, PyAny>)> {
    let mut has_long_integer = false;
    // The containers on the path to the item, outermost first, each with the stack's length when
    // it was entered - one met again inside itself never ends.
    let mut open_containers: Vec<(usize, usize)> = Vec::new();
    let mut pending: Vec<Bound<'py, PyAny>> = vec![value];
    while let Some(item) = pending.pop() {
        if let Ok(dict) = item.cast::<PyDict>() {
            if let Some(problem) = enter_container(&mut open_containers, &item, pending.len()) {
                return Ok((problem, py.None().into_bound(py)));
            }
            for key in dict.keys() {
                pending.push(key);
            }
            for dict_value in dict.values() {
                pending.push(dict_value);
            }
        } else if let Ok(list) = item.cast::<PyList>() {
            if let Some(problem) = enter_container(&mut open_containers, &item, pending.len()) {
                return Ok((problem, py.None().into_bound(py)));
            }
            for element in list.iter() {
                pending.push(element);
            }
        } else if let Ok(tuple) = item.cast::<PyTuple>() {
            if let Some(problem) = enter_container(&mut open_containers, &item, pending.len()) {
                return Ok((problem, py.None().into_bound(py)));
            }
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

/// Enters a container on the walk: None, with the container put on the path; `CIRCULAR_REFERENCE`
/// when the path already holds it - looked for only from `CIRCULAR_REFERENCE_CHECK_DEPTH` on, so a
/// value holding itself is told apart before it gets past `MAXIMUM_DEPTH`; `NESTED_TOO_DEEPLY`
/// when the path is that long.
///
/// A container's children are the stack's entries from the length it had when the container was
/// popped, so one entered at a longer stack than `pending_length` has been left - dropped here,
/// on entering the next container, which costs a scalar nothing.
fn enter_container(
    open_containers: &mut Vec<(usize, usize)>,
    container: &Bound<'_, PyAny>,
    pending_length: usize,
) -> Option<u8> {
    while open_containers.last().is_some_and(|&(_, entered_length)| entered_length > pending_length) {
        open_containers.pop();
    }
    let address = container.as_ptr() as usize;
    if open_containers.len() >= CIRCULAR_REFERENCE_CHECK_DEPTH
        && open_containers.iter().any(|&(open_address, _)| open_address == address)
    {
        return Some(CIRCULAR_REFERENCE);
    }
    if open_containers.len() >= MAXIMUM_DEPTH {
        return Some(NESTED_TOO_DEEPLY);
    }
    open_containers.push((address, pending_length));
    None
}
