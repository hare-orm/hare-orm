//! Reads the rows of a `union()` of model queries - each row an instance of the model its app and
//! model columns name.

use pyo3::exceptions::PyKeyError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};

use crate::python::ffi;
use crate::rows::model_reader::ModelReader;
use crate::rows::result_rows::ResultRows;

/// The instance of each row, in row order: `readers` maps an `(app, model)` pair to the index of
/// its `(reader, positions)` in `models` - the model's reader and the row positions of its columns.
/// A row naming no model raises `KeyError((app, model))`.
#[pyfunction]
#[pyo3(signature = (rows, readers, models, app_position, model_position, connection_alias=None))]
pub fn read_combined_rows<'py>(
    rows: &Bound<'py, PyAny>,
    readers: &Bound<'py, PyDict>,
    models: Vec<(Py<ModelReader>, Vec<usize>)>,
    app_position: usize,
    model_position: usize,
    connection_alias: Option<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyList>> {
    let py = rows.py();
    let rows = ResultRows::new(rows)?;
    let mut instances = ffi::ListBuilder::new(py, rows.len())?;
    for index in 0..rows.len() {
        let row = rows.row(index)?;
        let key = PyTuple::new(py, [row.get(app_position)?, row.get(model_position)?])?;
        let Some(model_index) = readers.get_item(&key)? else {
            return Err(PyKeyError::new_err(key.unbind()));
        };
        let (reader, positions) = &models[model_index.extract::<usize>()?];
        instances.set(index, reader.get().read_instance(py, &row, positions, connection_alias.as_ref())?);
    }
    Ok(instances.finish())
}
