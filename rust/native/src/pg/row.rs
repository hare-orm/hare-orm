//! `PgRow` - the row `fetch_all`/`fetch_one` return, readable by position and by column name at once
//! (`Features.supports_positional_rows`), as `asyncpg.Record` and `sqlite3.Row` are.

use std::collections::HashMap;
use std::sync::{Arc, OnceLock};

use pyo3::exceptions::{PyIndexError, PyKeyError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyInt, PyIterator, PyList, PyString};

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
    fn index_of(&self, py: Python<'_>, key: &str) -> Option<usize> {
        self.index
            .get_or_init(|| self.names.iter().enumerate().map(|(i, name)| (name.bind(py).to_string(), i)).collect())
            .get(key)
            .copied()
    }
}

#[pyclass(sequence, frozen)]
pub struct PgRow {
    names: Arc<RowNames>,
    values: Vec<Py<PyAny>>,
}

impl PgRow {
    pub fn new(names: Arc<RowNames>, values: Vec<Py<PyAny>>) -> Self {
        PgRow { names, values }
    }

    /// The value at `index`, None past the last column.
    pub fn get_value<'py>(&self, py: Python<'py>, index: usize) -> Option<Bound<'py, PyAny>> {
        self.values.get(index).map(|value| value.bind(py).clone())
    }

    fn index_of(&self, py: Python<'_>, key: &str) -> Option<usize> {
        self.names.index_of(py, key)
    }
}

#[pymethods]
impl PgRow {
    fn __len__(&self) -> usize {
        self.values.len()
    }

    /// `row[index]` (a negative index counts from the end, as for a tuple) or `row["column"]`.
    fn __getitem__(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        if key.is_instance_of::<PyInt>() {
            let idx: isize = key.extract()?;
            let len = self.values.len() as isize;
            let real_idx = if idx < 0 { idx + len } else { idx };
            if real_idx < 0 || real_idx >= len {
                return Err(PyIndexError::new_err("row index out of range"));
            }
            return Ok(self.values[real_idx as usize].clone_ref(py));
        }
        if let Ok(name) = key.cast::<PyString>() {
            let name = name.to_str()?;
            return match self.index_of(py, name) {
                Some(idx) => Ok(self.values[idx].clone_ref(py)),
                None => Err(PyKeyError::new_err(name.to_owned())),
            };
        }
        Err(PyTypeError::new_err("row indices must be int or str"))
    }

    /// The column names, in column order - what `dict(row)` reads.
    fn keys(&self, py: Python<'_>) -> Py<PyList> {
        PyList::new(py, self.names.names.iter().map(|n| n.bind(py)))
            .expect("names list is never huge enough to overflow")
            .unbind()
    }

    #[pyo3(signature = (key, default=None))]
    fn get(&self, py: Python<'_>, key: &str, default: Option<Py<PyAny>>) -> Py<PyAny> {
        match self.index_of(py, key) {
            Some(idx) => self.values[idx].clone_ref(py),
            None => default.unwrap_or_else(|| py.None()),
        }
    }

    fn values(&self, py: Python<'_>) -> Py<PyList> {
        PyList::new(py, self.values.iter().map(|v| v.bind(py)))
            .expect("values list is never huge enough to overflow")
            .unbind()
    }

    fn items(&self, py: Python<'_>) -> PyResult<Py<PyList>> {
        let list = PyList::empty(py);
        for (name, value) in self.names.names.iter().zip(self.values.iter()) {
            list.append((name.bind(py), value.bind(py)))?;
        }
        Ok(list.unbind())
    }

    fn __contains__(&self, py: Python<'_>, key: &str) -> bool {
        self.index_of(py, key).is_some()
    }

    /// Iterates the values, as a tuple does.
    fn __iter__<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyIterator>> {
        self.values(py).into_bound(py).try_iter()
    }

    fn __repr__(&self, py: Python<'_>) -> PyResult<String> {
        let mut parts = Vec::with_capacity(self.values.len());
        for (name, value) in self.names.names.iter().zip(self.values.iter()) {
            parts.push(format!("{}={}", name.bind(py).to_str()?, value.bind(py).repr()?));
        }
        Ok(format!("PgRow({})", parts.join(", ")))
    }

    /// The row as a real dict.
    fn to_dict(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let dict = PyDict::new(py);
        for (name, value) in self.names.names.iter().zip(self.values.iter()) {
            dict.set_item(name.bind(py), value.bind(py))?;
        }
        Ok(dict.unbind())
    }
}
