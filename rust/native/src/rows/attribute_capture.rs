//! Remembering attribute values of many instances in one call - `InstanceValues.capture()`.

use pyo3::exceptions::PyAttributeError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyInt, PyString, PyTuple};

/// Remembers the current value of each of `field_names` on each instance, keeping a value already
/// remembered: `old_values` maps `id(instance)` to `(instance, {field name: value})`; a field the
/// instance doesn't have is remembered as `missing`.
#[pyfunction]
pub fn capture_attribute_values(
    old_values: &Bound<'_, PyDict>,
    instances: &Bound<'_, PyAny>,
    field_names: Vec<Bound<'_, PyString>>,
    missing: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let py = old_values.py();
    for instance in instances.try_iter()? {
        let instance = instance?;
        // id() of an object is its address.
        let key = PyInt::new(py, instance.as_ptr() as usize);
        let values = if let Some(entry) = old_values.get_item(&key)? {
            entry.cast_into::<PyTuple>()?.get_item(1)?.cast_into::<PyDict>()?
        } else {
            let values = PyDict::new(py);
            old_values.set_item(&key, PyTuple::new(py, [instance.clone(), values.clone().into_any()])?)?;
            values
        };
        for name in &field_names {
            if values.contains(name)? {
                continue;
            }
            let value = match instance.getattr(name) {
                Ok(value) => value,
                Err(error) if error.is_instance_of::<PyAttributeError>(py) => missing.clone(),
                Err(error) => return Err(error),
            };
            values.set_item(name, value)?;
        }
    }
    Ok(())
}
