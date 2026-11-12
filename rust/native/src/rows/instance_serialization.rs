//! Row-ordered bulk serialization through the columns' writers.

use pyo3::prelude::*;
use pyo3::types::{PyList, PyString};

use crate::rows::typed_column_writer::TypedColumnWriter;

/// A column's writer - the native one called directly, any other through Python.
enum ColumnWriter<'py> {
    Typed(Bound<'py, TypedColumnWriter>),
    Python(Bound<'py, PyAny>),
}

/// The rows of `instances`: per instance, the value of each column through its writer -
/// `writer(value, instance)`.
#[pyfunction]
pub fn serialize_instances<'py>(
    instances: &Bound<'py, PyAny>,
    writers: Vec<(Bound<'py, PyString>, Bound<'py, PyAny>)>,
) -> PyResult<Bound<'py, PyList>> {
    let py = instances.py();
    let columns: Vec<(Bound<'py, PyString>, ColumnWriter<'py>)> = writers
        .into_iter()
        .map(|(name, writer)| {
            let column_writer = match writer.cast_into::<TypedColumnWriter>() {
                Ok(typed) => ColumnWriter::Typed(typed),
                Err(error) => ColumnWriter::Python(error.into_inner()),
            };
            (name, column_writer)
        })
        .collect();
    let rows = PyList::empty(py);
    let mut row_values = Vec::with_capacity(columns.len());
    for instance in instances.try_iter()? {
        let instance = instance?;
        row_values.clear();
        for (name, writer) in &columns {
            let value = instance.getattr(name)?;
            row_values.push(match writer {
                ColumnWriter::Typed(typed) => typed.get().write(&value, &instance)?,
                ColumnWriter::Python(writer) => writer.call1((&value, &instance))?,
            });
        }
        rows.append(PyList::new(py, &row_values)?)?;
    }
    Ok(rows)
}
