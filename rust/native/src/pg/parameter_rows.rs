//! `PgParameterRows` - the parameter values of one statement run once per row (`execute_many()`),
//! made by `ModelWriter` straight from the attribute values.

use pyo3::prelude::*;

use crate::pg::value::Value;

#[pyclass(frozen, sequence, module = "rust.native.pg")]
pub struct PgParameterRows {
    /// The rows - or the error reading one of their values gave, raised when a statement binds them.
    rows: Result<Vec<Vec<Value>>, PyErr>,
    length: usize,
}

impl PgParameterRows {
    pub fn new(rows: Result<Vec<Vec<Value>>, PyErr>, length: usize) -> Self {
        PgParameterRows { rows, length }
    }

    /// The rows, for one statement.
    pub fn get_rows(&self, py: Python<'_>) -> PyResult<Vec<Vec<Value>>> {
        match &self.rows {
            Ok(rows) => Ok(rows.clone()),
            Err(error) => Err(error.clone_ref(py)),
        }
    }
}

#[pymethods]
impl PgParameterRows {
    fn __len__(&self) -> usize {
        self.length
    }
}
