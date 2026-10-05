//! `copy.deepcopy()` of a JSON-shaped value, made here: dicts, lists and tuples whose leaves are
//! str, int, float, bool, bytes or None.

use std::collections::HashMap;

use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

/// How deep a value is copied here - a deeper one goes to `copy.deepcopy()`.
const MAXIMUM_DEPTH: usize = 200;

/// Copies already made, by the address of their original - a value held twice is copied once, as
/// `copy.deepcopy()`'s memo does.
type Memo<'py> = HashMap<usize, Bound<'py, PyAny>>;

/// A deep copy of `value` with `copy.deepcopy()`'s result, None when the value holds anything but
/// containers and leaves this copies.
pub fn deep_copy_plain<'py>(value: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
    copy_value(value, &mut Memo::new(), 0)
}

fn is_atomic(value: &Bound<'_, PyAny>) -> bool {
    value.is_none()
        || value.is_exact_instance_of::<PyString>()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyFloat>()
        || value.is_exact_instance_of::<PyBool>()
        || value.is_exact_instance_of::<PyBytes>()
}

fn copy_value<'py>(
    value: &Bound<'py, PyAny>,
    memo: &mut Memo<'py>,
    depth: usize,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    if is_atomic(value) {
        return Ok(Some(value.clone()));
    }
    if depth >= MAXIMUM_DEPTH {
        return Ok(None);
    }
    let address = value.as_ptr() as usize;
    if let Some(copy) = memo.get(&address) {
        return Ok(Some(copy.clone()));
    }
    let py = value.py();
    if let Ok(dict) = value.cast_exact::<PyDict>() {
        let copy = PyDict::new(py);
        memo.insert(address, copy.clone().into_any());
        for (key, item) in dict.iter() {
            let (Some(key), Some(item)) = (copy_value(&key, memo, depth + 1)?, copy_value(&item, memo, depth + 1)?)
            else {
                return Ok(None);
            };
            copy.set_item(key, item)?;
        }
        return Ok(Some(copy.into_any()));
    }
    if let Ok(list) = value.cast_exact::<PyList>() {
        let copy = PyList::empty(py);
        memo.insert(address, copy.clone().into_any());
        for item in list.iter() {
            let Some(item) = copy_value(&item, memo, depth + 1)? else {
                return Ok(None);
            };
            copy.append(item)?;
        }
        return Ok(Some(copy.into_any()));
    }
    if let Ok(tuple) = value.cast_exact::<PyTuple>() {
        let mut items = Vec::with_capacity(tuple.len());
        let mut all_same = true;
        for item in tuple.iter() {
            let Some(copy) = copy_value(&item, memo, depth + 1)? else {
                return Ok(None);
            };
            all_same &= copy.is(&item);
            items.push(copy);
        }
        // A tuple whose items copy to themselves is its own copy.
        let copy = if all_same { value.clone() } else { PyTuple::new(py, items)?.into_any() };
        memo.insert(address, copy.clone());
        return Ok(Some(copy));
    }
    Ok(None)
}
