//! Physical row selection and conversion before logical composite grouping.

use pyo3::prelude::*;
use pyo3::types::PyTuple;
use pyo3::{PyTraverseError, PyVisit};

use crate::rows::column_reader::ColumnReader;

#[pyclass(frozen, module = "rust.native.rows")]
pub struct ValueLayout {
    columns: Vec<(Py<PyAny>, ColumnReader)>,
    groups: Vec<usize>,
}

#[pymethods]
impl ValueLayout {
    #[new]
    pub fn new(raw_columns: Vec<(Bound<'_, PyAny>, Bound<'_, PyAny>)>, groups: Vec<usize>) -> PyResult<Self> {
        let columns: Vec<_> =
            raw_columns.into_iter().map(|(key, reader)| (key.unbind(), ColumnReader::new(reader))).collect();
        if !groups.is_empty() && (groups.contains(&0) || groups.iter().sum::<usize>() != columns.len()) {
            return Err(pyo3::exceptions::PyValueError::new_err("composite groups do not cover the selected columns"));
        }
        Ok(Self { columns, groups })
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        for (key, reader) in &self.columns {
            visit.call(key)?;
            reader.traverse(&visit)?;
        }
        Ok(())
    }

    fn read<'py>(&self, row: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyTuple>> {
        PyTuple::new(row.py(), self.read_values(row)?)
    }
}

impl ValueLayout {
    /// The row's values in output order - composite groups collapsed - as a vector: a dict, a tuple or a
    /// flat value is made of it without an intermediate tuple.
    pub fn read_values<'py>(&self, row: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
        let py = row.py();
        // Fetch every physical value before callbacks, matching itemgetter's error ordering.
        let mut values = Vec::with_capacity(self.columns.len());
        for (key, _) in &self.columns {
            values.push(row.get_item(key.bind(py))?);
        }
        for (value, (_, reader)) in values.iter_mut().zip(&self.columns) {
            if !matches!(reader, ColumnReader::AsIs) {
                *value = reader.read(value.clone())?;
            }
        }
        if self.groups.is_empty() {
            return Ok(values);
        }
        let mut position = 0;
        for (group, width) in self.groups.iter().enumerate() {
            let components = &values[position..position + width];
            let grouped_value = if *width == 1 {
                components[0].clone()
            } else if components.iter().all(Bound::is_none) {
                py.None().into_bound(py)
            } else {
                PyTuple::new(py, components)?.into_any()
            };
            values[group] = grouped_value;
            position += width;
        }
        values.truncate(self.groups.len());
        Ok(values)
    }
}
