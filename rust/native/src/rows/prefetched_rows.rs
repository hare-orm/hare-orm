//! Laying prefetched rows out on the instances they belong to - `Prefetcher`'s per-row loops in
//! one call each.

use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyList, PyString};

use crate::python::ffi::{generic_delete_attribute, generic_set_attribute};

/// The entries grouped by the value of `attribute` on each, in their order: `{value: [entry, ...]}`.
#[pyfunction]
pub fn group_by_attribute<'py>(
    entries: &Bound<'py, PyAny>,
    attribute: &Bound<'py, PyString>,
) -> PyResult<Bound<'py, PyDict>> {
    let groups = PyDict::new(entries.py());
    for entry in entries.try_iter()? {
        let entry = entry?;
        append_to_group(&groups, entry.getattr(attribute)?, entry)?;
    }
    Ok(groups)
}

/// Appends `value` to the list `groups` holds under `key`, starting the list when there is none.
fn append_to_group<'py>(groups: &Bound<'py, PyDict>, key: Bound<'py, PyAny>, value: Bound<'py, PyAny>) -> PyResult<()> {
    match groups.get_item(&key)? {
        Some(group) => group.cast_into::<PyList>()?.append(value),
        None => groups.set_item(key, PyList::new(groups.py(), [value])?),
    }
}

/// The items at `value_index` of the rows grouped by the item at `key_index`, in row order:
/// `{row[key_index]: [row[value_index], ...]}`.
#[pyfunction]
pub fn group_by_item<'py>(
    rows: &Bound<'py, PyAny>,
    key_index: usize,
    value_index: usize,
) -> PyResult<Bound<'py, PyDict>> {
    let groups = PyDict::new(rows.py());
    for row in rows.try_iter()? {
        let row = row?;
        append_to_group(&groups, row.get_item(key_index)?, row.get_item(value_index)?)?;
    }
    Ok(groups)
}

/// The entries grouped by each owner `owners_by_key` lists under the entry's `key_attribute`
/// value, in entry order: `{owner: [entry, ...]}` - an entry several owners share is in each
/// owner's list.
#[pyfunction]
pub fn group_related<'py>(
    entries: &Bound<'py, PyAny>,
    key_attribute: &Bound<'py, PyString>,
    owners_by_key: &Bound<'py, PyDict>,
) -> PyResult<Bound<'py, PyDict>> {
    let groups = PyDict::new(entries.py());
    for entry in entries.try_iter()? {
        let entry = entry?;
        let Some(owners) = owners_by_key.get_item(entry.getattr(key_attribute)?)? else {
            continue;
        };
        for owner in owners.try_iter()? {
            append_to_group(&groups, owner?, entry.clone())?;
        }
    }
    Ok(groups)
}

/// Groups related rows read once per owner - each carrying its owner's key in `owner_attribute` -
/// by that owner, in row order: `{owner: [entry, ...]}`. The rows of one related object (equal
/// `key_attribute` values) are one instance in every owner's list - the first row's - and
/// `owner_attribute` is removed from it.
#[pyfunction]
pub fn group_rows_by_owner<'py>(
    entries: &Bound<'py, PyAny>,
    key_attribute: &Bound<'py, PyString>,
    owner_attribute: &Bound<'py, PyString>,
) -> PyResult<Bound<'py, PyDict>> {
    let py = entries.py();
    let groups = PyDict::new(py);
    let shared_entries = PyDict::new(py);
    for entry in entries.try_iter()? {
        let entry = entry?;
        let owner = entry.getattr(owner_attribute)?;
        let key = entry.getattr(key_attribute)?;
        let shared_entry = if let Some(shared_entry) = shared_entries.get_item(&key)? {
            shared_entry
        } else {
            shared_entries.set_item(key, &entry)?;
            entry
        };
        append_to_group(&groups, owner, shared_entry)?;
    }
    for shared_entry in shared_entries.values() {
        generic_delete_attribute(&shared_entry, owner_attribute)?;
    }
    Ok(groups)
}

/// Gives each instance's to-many relation the rows of `rows_by_key` under the instance's
/// `key_attribute` value - none when there are none: the relation kept in `relation_attribute`,
/// made by `make_relation(instance)` and kept there when the instance has none yet, is marked
/// fetched with those rows.
#[pyfunction]
pub fn set_prefetched_rows(
    instances: &Bound<'_, PyAny>,
    key_attribute: &Bound<'_, PyString>,
    rows_by_key: &Bound<'_, PyDict>,
    relation_attribute: &Bound<'_, PyString>,
    make_relation: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let py = instances.py();
    let fetched_name = pyo3::intern!(py, "_fetched");
    let rows_name = pyo3::intern!(py, "related_objects");
    let fetched = PyBool::new(py, true).to_owned().into_any();
    for instance in instances.try_iter()? {
        let instance = instance?;
        let key = instance.getattr(key_attribute)?;
        let rows = match rows_by_key.get_item(&key)? {
            Some(rows) => rows,
            None => PyList::empty(py).into_any(),
        };
        let relation = match instance.getattr_opt(relation_attribute)? {
            Some(relation) if !relation.is_none() => relation,
            _ => {
                let relation = make_relation.call1((&instance,))?;
                generic_set_attribute(&instance, relation_attribute, &relation)?;
                relation
            }
        };
        generic_set_attribute(&relation, fetched_name, &fetched)?;
        generic_set_attribute(&relation, rows_name, &rows)?;
    }
    Ok(())
}
