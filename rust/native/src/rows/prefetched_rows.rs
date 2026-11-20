//! Group prefetched rows by their relation keys without publishing them on parent instances.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString};

/// The entries grouped by their value of `attribute`, in entry order: `{value: [entry, ...]}` - or,
/// without `many`, `{value: entry}`, the last entry of a value.
#[pyfunction]
pub fn group_by_attribute<'py>(
    entries: &Bound<'py, PyAny>,
    attribute: &Bound<'py, PyString>,
    many: bool,
) -> PyResult<Bound<'py, PyDict>> {
    let groups = PyDict::new(entries.py());
    for entry in entries.try_iter()? {
        let entry = entry?;
        let key = entry.getattr(attribute)?;
        if many {
            append_to_group(&groups, key, entry)?;
        } else {
            groups.set_item(key, entry)?;
        }
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
