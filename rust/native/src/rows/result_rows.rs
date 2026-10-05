//! `ResultRows` - the rows a reader reads: a `PgResult`, each value read straight from what the
//! driver decoded, or any other sequence of driver rows (`asyncpg.Record`, `sqlite3.Row`, `PgRow`),
//! each value read from its Python object.

use pyo3::exceptions::PyIndexError;
use pyo3::prelude::*;
use pyo3::types::PyList;

use crate::pg::result::PgResult;
use crate::rows::result_row::ResultRow;

pub enum ResultRows<'py> {
    Decoded(Bound<'py, PgResult>),
    Objects(Bound<'py, PyList>),
}

impl<'py> ResultRows<'py> {
    /// The rows of `rows` - a `PgResult`, a list, or any other sequence (read into a list).
    pub fn new(rows: &Bound<'py, PyAny>) -> PyResult<Self> {
        if let Ok(result) = rows.cast::<PgResult>() {
            return Ok(ResultRows::Decoded(result.clone()));
        }
        if let Ok(list) = rows.cast::<PyList>() {
            return Ok(ResultRows::Objects(list.clone()));
        }
        Ok(ResultRows::Objects(PyList::new(rows.py(), rows.try_iter()?.collect::<PyResult<Vec<_>>>()?)?))
    }

    pub fn len(&self) -> usize {
        match self {
            ResultRows::Decoded(result) => result.get().len(),
            ResultRows::Objects(list) => list.len(),
        }
    }

    /// Row `index`.
    pub fn row(&self, index: usize) -> PyResult<ResultRow<'_, 'py>> {
        match self {
            ResultRows::Decoded(result) => {
                if index >= result.get().len() {
                    return Err(PyIndexError::new_err("result index out of range"));
                }
                Ok(ResultRow::Decoded { py: result.py(), result: result.get(), index })
            }
            ResultRows::Objects(list) => Ok(ResultRow::Object(list.get_item(index)?)),
        }
    }
}
