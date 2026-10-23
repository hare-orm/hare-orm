//! `encode_json()` - the text a `JSONField` writes for a value made only of dicts, lists, tuples, str,
//! int, float, bool and None, as orjson writes it, with what a JSON column can't store checked on the
//! same pass.

use pyo3::prelude::*;
use pyo3::types::PyString;

use crate::codecs::json;

/// The JSON text of `value`; None when the value holds anything else, a null byte, NaN or infinity,
/// an int past 64 bits or deep nesting - the field's own encoding then decides.
#[pyfunction]
pub fn encode_json<'py>(value: &Bound<'py, PyAny>) -> Option<Bound<'py, PyString>> {
    json::encode(value).map(|text| PyString::new(value.py(), &text))
}
