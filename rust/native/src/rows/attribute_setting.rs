//! Setting attributes of many instances in one call - what `object.__setattr__` does for each.

use pyo3::prelude::*;
use pyo3::types::PyString;

use crate::python::ffi::generic_set_attribute;

/// Sets each of `names` to the value at its position in `values` on every instance of `instances`,
/// past any `__setattr__` the instances' class defines.
#[pyfunction]
pub fn set_attribute_values(
    instances: &Bound<'_, PyAny>,
    names: Vec<Bound<'_, PyString>>,
    values: Vec<Bound<'_, PyAny>>,
) -> PyResult<()> {
    if names.len() != values.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("names and values differ in length"));
    }
    for instance in instances.try_iter()? {
        let instance = instance?;
        for (name, value) in names.iter().zip(&values) {
            generic_set_attribute(&instance, name, value)?;
        }
    }
    Ok(())
}
