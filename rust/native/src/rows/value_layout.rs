//! Physical row selection and conversion before logical composite grouping.

use pyo3::intern;
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
    pub fn new(layout: &Bound<'_, PyAny>) -> PyResult<Self> {
        let py = layout.py();
        let raw_columns = layout.getattr(intern!(py, "columns"))?.cast_into::<PyTuple>()?;
        let mut columns = Vec::with_capacity(raw_columns.len());
        for column in raw_columns.iter() {
            columns.push((
                column.getattr(intern!(py, "key"))?.unbind(),
                ColumnReader::new(column.getattr(intern!(py, "reader"))?),
            ));
        }
        let groups: Vec<usize> = layout.getattr(intern!(py, "groups"))?.extract()?;
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
}

impl ValueLayout {
    pub fn read<'py>(&self, row: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyTuple>> {
        PyTuple::new(row.py(), self.read_values(row)?)
    }

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
        let mut grouped = Vec::with_capacity(self.groups.len());
        let mut position = 0;
        for width in &self.groups {
            let components = &values[position..position + width];
            grouped.push(if *width == 1 {
                components[0].clone()
            } else if components.iter().all(Bound::is_none) {
                py.None().into_bound(py)
            } else {
                PyTuple::new(py, components)?.into_any()
            });
            position += width;
        }
        Ok(grouped)
    }
}

#[pyfunction]
pub fn read_row_values<'py>(row: &Bound<'py, PyAny>, layout: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyTuple>> {
    layout.getattr(intern!(layout.py(), "native_layout"))?.cast_into::<ValueLayout>()?.get().read(row)
}
