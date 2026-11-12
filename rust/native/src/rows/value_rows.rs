//! Values result shapes assembled with one layout for each batch.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};

use crate::rows::value_layout::ValueLayout;

/// The row of the output shape, made straight from the row's values.
fn make_row<'py>(
    values: Vec<Bound<'py, PyAny>>,
    row_type: &str,
    names: &Bound<'py, PyTuple>,
    row_class: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = names.py();
    if !row_class.is_none() {
        return row_class.call1(PyTuple::new(py, values)?);
    }
    match row_type {
        "dict" => {
            if names.len() != values.len() {
                return Err(pyo3::exceptions::PyValueError::new_err("output names and values differ in length"));
            }
            let result = PyDict::new(py);
            for (name, value) in names.iter().zip(values) {
                result.set_item(name, value)?;
            }
            Ok(result.into_any())
        }
        "flat" => values
            .into_iter()
            .next()
            .ok_or_else(|| pyo3::exceptions::PyIndexError::new_err("a flat row holds no value")),
        "tuple" => Ok(PyTuple::new(py, values)?.into_any()),
        _ => Err(pyo3::exceptions::PyValueError::new_err("unknown row type")),
    }
}

#[pyfunction]
pub fn read_values_row<'py>(
    row: &Bound<'py, PyAny>,
    layout: &Bound<'py, PyAny>,
    row_type: &str,
    names: &Bound<'py, PyTuple>,
    row_class: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let layout = layout.getattr(intern!(layout.py(), "native_layout"))?.cast_into::<ValueLayout>()?;
    make_row(layout.get().read_values(row)?, row_type, names, row_class)
}

#[pyfunction]
pub fn read_values_batch<'py>(
    rows: &Bound<'py, PyAny>,
    layout: &Bound<'py, PyAny>,
    row_type: &str,
    names: &Bound<'py, PyTuple>,
    row_class: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyList>> {
    let layout = layout.getattr(intern!(layout.py(), "native_layout"))?.cast_into::<ValueLayout>()?;
    let result = PyList::empty(rows.py());
    for row in rows.try_iter()? {
        result.append(make_row(layout.get().read_values(&row?)?, row_type, names, row_class)?)?;
    }
    Ok(result)
}
