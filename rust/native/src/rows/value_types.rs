//! Checking the types of a row of values in one call.

use pyo3::prelude::*;
use pyo3::types::{PyList, PyTuple};

/// Whether every value of `values` is exactly of one of `types`.
#[pyfunction]
pub fn are_all_of_types(values: &Bound<'_, PyList>, types: &Bound<'_, PyTuple>) -> bool {
    values.iter().all(|value| {
        let value_type = value.get_type();
        types.iter().any(|allowed_type| value_type.is(&allowed_type))
    })
}
