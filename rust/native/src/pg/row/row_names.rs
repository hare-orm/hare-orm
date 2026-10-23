use pyo3::types::PyString;
use std::collections::HashMap;
use std::sync::OnceLock;

use pyo3::prelude::*;

/// The column names every row of one result shares through an `Arc`.
pub struct RowNames {
    pub names: Vec<Py<PyString>>,
    /// Name -> position, built on the first by-name read: a positional-only fetch never needs it,
    /// and `dict(row)` would scan the names once per key without it.
    index: OnceLock<HashMap<String, usize>>,
}

impl RowNames {
    pub fn new(names: Vec<Py<PyString>>) -> Self {
        RowNames { names, index: OnceLock::new() }
    }

    /// The position of column `key` - the last of duplicate names (`SELECT 1 AS id, 2 AS id`), as
    /// `asyncpg.Record` reads it.
    pub(super) fn index_of(&self, py: Python<'_>, key: &str) -> Option<usize> {
        self.index
            .get_or_init(|| {
                self.names.iter().enumerate().map(|(index, name)| (name.bind(py).to_string(), index)).collect()
            })
            .get(key)
            .copied()
    }
}
