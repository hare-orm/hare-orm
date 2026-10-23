//! Builds the rows of `values()`/`values_list()` from driver rows - a tuple, a dict, a single value
//! or a namedtuple per row, each column read by its codec.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple, PyType};
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::field_codec::FieldCodec;
use crate::python::ffi;
use crate::rows::result_row::ResultRow;
use crate::rows::result_rows::ResultRows;

/// The selected columns of a values query, and how each is read.
#[pyclass(frozen, module = "rust.native.rows")]
pub struct ValuesReader {
    codecs: Vec<Py<FieldCodec>>,
}

impl ValuesReader {
    fn read_values<'py>(&self, py: Python<'py>, row: &ResultRow<'_, 'py>) -> PyResult<Bound<'py, PyTuple>> {
        let mut values = Vec::with_capacity(self.codecs.len());
        for (index, codec) in self.codecs.iter().enumerate() {
            values.push(row.read(index, codec.get())?);
        }
        PyTuple::new(py, values)
    }
}

#[pymethods]
impl ValuesReader {
    /// A reader of `codecs`, one per selected column in selection order.
    #[new]
    fn new(codecs: Vec<Py<FieldCodec>>) -> Self {
        ValuesReader { codecs }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        for codec in &self.codecs {
            visit.call(codec)?;
        }
        Ok(())
    }

    /// A tuple of the column values of each row of `rows` - a `PgResult` or a sequence of driver
    /// rows, as for every method here.
    fn read_tuples<'py>(&self, rows: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyList>> {
        let py = rows.py();
        let rows = ResultRows::new(rows)?;
        let mut tuples = ffi::ListBuilder::new(py, rows.len())?;
        for index in 0..rows.len() {
            tuples.set(index, self.read_values(py, &rows.row(index)?)?.into_any());
        }
        Ok(tuples.finish())
    }

    /// The value of the first column of each row.
    fn read_flat<'py>(&self, rows: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyList>> {
        let py = rows.py();
        let rows = ResultRows::new(rows)?;
        let codec = self.codecs.first().map(Py::get);
        let mut values = ffi::ListBuilder::new(py, rows.len())?;
        for index in 0..rows.len() {
            let row = rows.row(index)?;
            values.set(
                index,
                match codec {
                    Some(codec) => row.read(0, codec)?,
                    None => row.get(0)?,
                },
            );
        }
        Ok(values.finish())
    }

    /// A dict of each row's column values under `keys`, one per column.
    fn read_dicts<'py>(
        &self,
        rows: &Bound<'py, PyAny>,
        keys: Vec<Bound<'py, PyString>>,
    ) -> PyResult<Bound<'py, PyList>> {
        let py = rows.py();
        let rows = ResultRows::new(rows)?;
        let mut dicts = ffi::ListBuilder::new(py, rows.len())?;
        for index in 0..rows.len() {
            let row = rows.row(index)?;
            let dict = PyDict::new(py);
            for (column, (codec, key)) in self.codecs.iter().zip(&keys).enumerate() {
                dict.set_item(key, row.read(column, codec.get())?)?;
            }
            dicts.set(index, dict.into_any());
        }
        Ok(dicts.finish())
    }

    /// Sets the value of each column, read from row position `positions[column]`, on the instance
    /// of the row under the column's name; a row whose instance is None is skipped.
    fn set_attributes(
        &self,
        instances: &Bound<'_, PyList>,
        rows: &Bound<'_, PyAny>,
        positions: Vec<usize>,
    ) -> PyResult<()> {
        let py = rows.py();
        let rows = ResultRows::new(rows)?;
        for (index, instance) in instances.iter().enumerate().take(rows.len()) {
            if instance.is_none() {
                continue;
            }
            let row = rows.row(index)?;
            for (codec, position) in self.codecs.iter().zip(&positions) {
                let codec = codec.get();
                let value = row.read(*position, codec)?;
                ffi::generic_set_attribute(&instance, codec.name.bind(py), &value)?;
            }
        }
        Ok(())
    }

    /// An instance of `row_class`, a tuple subclass, holding each row's column values - built as
    /// `tuple.__new__(row_class, values)`, without the class's own `__new__`.
    fn read_named<'py>(
        &self,
        rows: &Bound<'py, PyAny>,
        row_class: &Bound<'py, PyType>,
    ) -> PyResult<Bound<'py, PyList>> {
        let py = rows.py();
        let rows = ResultRows::new(rows)?;
        let tuple_new = py.get_type::<PyTuple>().getattr(intern!(py, "__new__"))?;
        let mut named = ffi::ListBuilder::new(py, rows.len())?;
        for index in 0..rows.len() {
            named.set(index, tuple_new.call1((row_class, self.read_values(py, &rows.row(index)?)?))?);
        }
        Ok(named.finish())
    }
}
