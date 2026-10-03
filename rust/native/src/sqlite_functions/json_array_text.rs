//! The JSON array text a long `__in` list of integers and strings binds as on SQLite - one parameter
//! `json_each()` reads; a list holding anything else goes to the Python function.

use std::fmt::Write;

use pyo3::prelude::*;
use pyo3::types::{PyInt, PyList, PyString};

use crate::python::ffi::get_i64;
use crate::sqlite_functions::json_canonical::write_json_string;

/// The JSON array of a list of exact integers within 64 bits and exact strings; None for a list
/// holding any other value.
#[pyfunction]
pub fn get_json_array_text(values: &Bound<'_, PyList>) -> Option<String> {
    let mut text = String::with_capacity(values.len() * 4 + 2);
    text.push('[');
    for (index, value) in values.iter().enumerate() {
        if index > 0 {
            text.push(',');
        }
        if value.is_exact_instance_of::<PyInt>() {
            let _ = write!(text, "{}", get_i64(&value)?);
        } else if value.is_exact_instance_of::<PyString>() {
            write_json_string(value.cast::<PyString>().ok()?.to_str().ok()?, &mut text);
        } else {
            return None;
        }
    }
    text.push(']');
    Some(text)
}
