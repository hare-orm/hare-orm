//! `PgResult` - the rows of one result as the driver read them. Nothing in it is a Python object
//! until it is read as a sequence of `PgRow`s. Each row is built on first use from its decoded values.

use std::sync::Arc;

use pyo3::exceptions::PyIndexError;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyIterator, PyList, PySlice, PyTuple};

use crate::pg::result_columns::ResultColumns;
use crate::pg::result_data::ResultData;
use crate::pg::row::PgRow;

#[pyclass(frozen, sequence, module = "rust.native.pg")]
pub struct PgResult {
    columns: Arc<ResultColumns>,
    data: ResultData,
    /// The `PgRow` of each row read as one, built on first use - a row read twice is one object, as
    /// in a list.
    row_objects: PyOnceLock<Box<[PyOnceLock<Py<PgRow>>]>>,
}

impl PgResult {
    pub fn new(columns: Arc<ResultColumns>, data: ResultData) -> Self {
        PgResult { columns, data, row_objects: PyOnceLock::new() }
    }

    pub fn len(&self) -> usize {
        self.data.len()
    }

    pub fn is_empty(&self) -> bool {
        self.data.is_empty()
    }

    /// The `PgRow` of row `index`.
    pub fn get_row<'py>(&self, py: Python<'py>, index: usize) -> PyResult<Bound<'py, PgRow>> {
        let row_objects = self.row_objects.get_or_init(py, || (0..self.len()).map(|_| PyOnceLock::new()).collect());
        let row = row_objects[index].get_or_try_init(py, || {
            Py::new(py, PgRow::from_values(py, self.columns.get_row_names(py).clone(), &self.data.rows[index])?)
        })?;
        Ok(row.bind(py).clone())
    }

    /// Every row as a `PgRow`, in a list.
    fn get_rows<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let rows = (0..self.len()).map(|index| self.get_row(py, index)).collect::<PyResult<Vec<_>>>()?;
        PyList::new(py, rows)
    }
}

#[pymethods]
impl PgResult {
    /// Positional rows use the same decoded values as named rows, without a `PgRow` wrapper.
    /// Column names accompany the tuples so callers need not inspect a first row.
    fn positional<'py>(&self, py: Python<'py>) -> PyResult<(Bound<'py, PyList>, Bound<'py, PyTuple>)> {
        let rows = PyList::empty(py);
        for values in &self.data.rows {
            rows.append(PyTuple::new(py, values.iter())?)?;
        }
        Ok((rows, PyTuple::new(py, self.columns.names())?))
    }

    /// The server's command completion count, including writes without RETURNING.
    #[getter]
    fn row_count(&self) -> Option<u64> {
        self.data.row_count
    }

    fn __len__(&self) -> usize {
        self.len()
    }

    /// `result[index]` (a negative index counts from the end) or `result[start:stop:step]`, a list -
    /// as for a list of rows.
    fn __getitem__<'py>(&self, py: Python<'py>, key: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        if let Ok(slice) = key.cast::<PySlice>() {
            let indices = slice.indices(self.len() as isize)?;
            let mut rows = Vec::with_capacity(indices.slicelength);
            let mut index = indices.start;
            for _ in 0..indices.slicelength {
                rows.push(self.get_row(py, index as usize)?);
                index += indices.step;
            }
            return Ok(PyList::new(py, rows)?.into_any());
        }
        let index: isize = key.extract()?;
        let length = self.len() as isize;
        let position = if index < 0 { index + length } else { index };
        if position < 0 || position >= length {
            return Err(PyIndexError::new_err("result index out of range"));
        }
        Ok(self.get_row(py, position as usize)?.into_any())
    }

    fn __iter__<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyIterator>> {
        self.get_rows(py)?.try_iter()
    }

    /// Equal to a sequence of the same rows - another result or a list.
    fn __eq__(&self, py: Python<'_>, other: &Bound<'_, PyAny>) -> PyResult<bool> {
        let other = match other.cast::<PgResult>() {
            Ok(result) => result.get().get_rows(py)?.into_any(),
            Err(_) => other.clone(),
        };
        self.get_rows(py)?.eq(other)
    }

    fn __repr__(&self, py: Python<'_>) -> PyResult<String> {
        Ok(format!("PgResult({})", self.get_rows(py)?.repr()?))
    }
}
