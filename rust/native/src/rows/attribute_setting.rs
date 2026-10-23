//! Setting attributes of many instances in one call - what `object.__setattr__` does for each.

use pyo3::exceptions::PyAttributeError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};

use crate::python::ffi::generic_set_attribute;
use crate::rows::attribute_capture::capture_instance_values;

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

/// Sets each of `names` to the value at its position in `values` on every instance of `instances` whose
/// attribute holds None or is missing, past any `__setattr__` the instances' class defines - a stamp an
/// instance gets only when it holds no value of its own.
#[pyfunction]
pub fn set_missing_attribute_values(
    instances: &Bound<'_, PyAny>,
    names: Vec<Bound<'_, PyString>>,
    values: Vec<Bound<'_, PyAny>>,
) -> PyResult<()> {
    if names.len() != values.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("names and values differ in length"));
    }
    let py = instances.py();
    for instance in instances.try_iter()? {
        let instance = instance?;
        for (name, value) in names.iter().zip(&values) {
            let holds_value = match instance.getattr(name) {
                Ok(current) => !current.is_none(),
                Err(error) if error.is_instance_of::<PyAttributeError>(py) => false,
                Err(error) => return Err(error),
            };
            if !holds_value {
                generic_set_attribute(&instance, name, value)?;
            }
        }
    }
    Ok(())
}

/// Stamps every instance of `instances` with one value, past any `__setattr__` the instances' class
/// defines: each of `always_names` is set, each of `unset_names` only while it holds None or is missing.
/// The values of `saved_names` before it are remembered first in `old_values`, as
/// `capture_attribute_values` remembers them - a failed write puts them back.
#[pyfunction]
pub fn stamp_attribute_values(
    old_values: &Bound<'_, PyDict>,
    instances: &Bound<'_, PyAny>,
    saved_names: Vec<Bound<'_, PyString>>,
    always_names: Vec<Bound<'_, PyString>>,
    unset_names: Vec<Bound<'_, PyString>>,
    value: &Bound<'_, PyAny>,
    missing: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let py = instances.py();
    for instance in instances.try_iter()? {
        let instance = instance?;
        capture_instance_values(old_values, &instance, &saved_names, missing)?;
        for name in &always_names {
            generic_set_attribute(&instance, name, value)?;
        }
        for name in &unset_names {
            let holds_value = match instance.getattr(name) {
                Ok(current) => !current.is_none(),
                Err(error) if error.is_instance_of::<PyAttributeError>(py) => false,
                Err(error) => return Err(error),
            };
            if !holds_value {
                generic_set_attribute(&instance, name, value)?;
            }
        }
    }
    Ok(())
}
