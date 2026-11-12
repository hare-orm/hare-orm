//! Remembering attribute values of many instances in one call - `InstanceValues.capture()` and
//! `InstanceSnapshot.capture_persistence()`.

use pyo3::exceptions::PyAttributeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyInt, PyList, PyString, PyTuple};

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
    for instance in instances.try_iter()? {
        capture_instance_values(old_values, &instance?, &field_names, missing)?;
    }
    Ok(())
}

/// Remembers the current value of each of `field_names` on one instance - see `capture_attribute_values`.
pub fn capture_instance_values(
    old_values: &Bound<'_, PyDict>,
    instance: &Bound<'_, PyAny>,
    field_names: &[Bound<'_, PyString>],
    missing: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let py = old_values.py();
    // id() of an object is its address.
    let key = PyInt::new(py, instance.as_ptr() as usize);
    let values = if let Some(entry) = old_values.get_item(&key)? {
        entry.cast_into::<PyTuple>()?.get_item(1)?.cast_into::<PyDict>()?
    } else {
        let values = PyDict::new(py);
        old_values.set_item(&key, PyTuple::new(py, [instance.clone(), values.clone().into_any()])?)?;
        values
    };
    for name in field_names {
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
    Ok(())
}

/// The persistence snapshots of `instances`, in order - `snapshot_class(instance, values)` of each:
/// the instance's own (`__dict__`) values of `names` - `missing` for one it lacks, a dict copied, as a
/// later write edits it in place - and then its `pk` when `includes_primary_key`.
#[pyfunction]
pub fn capture_persistence_snapshots<'py>(
    snapshot_class: &Bound<'py, PyAny>,
    instances: &Bound<'py, PyAny>,
    names: Vec<Bound<'py, PyString>>,
    missing: &Bound<'py, PyAny>,
    includes_primary_key: bool,
) -> PyResult<Bound<'py, PyList>> {
    let py = snapshot_class.py();
    let state_name = intern!(py, "__dict__");
    let primary_key_name = intern!(py, "pk");
    let snapshots = PyList::empty(py);
    for instance in instances.try_iter()? {
        let instance = instance?;
        let state = instance.getattr(state_name)?.cast_into::<PyDict>()?;
        let values = PyDict::new(py);
        for name in &names {
            let value = match state.get_item(name)? {
                Some(value) => match value.cast::<PyDict>() {
                    Ok(mapping) => mapping.copy()?.into_any(),
                    Err(_) => value,
                },
                None => missing.clone(),
            };
            values.set_item(name, value)?;
        }
        if includes_primary_key {
            values.set_item(primary_key_name, instance.getattr(primary_key_name)?)?;
        }
        snapshots.append(snapshot_class.call1((instance, values))?)?;
    }
    Ok(snapshots)
}
