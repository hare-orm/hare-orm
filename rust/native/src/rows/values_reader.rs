//! Builds the rows of `values()`/`values_list()` from driver rows - a tuple, a dict, a single value
//! or a namedtuple per row, each column read by its codec.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple, PyType};
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::field_codec::FieldCodec;
use crate::python::ffi;

/// The selected columns of a values query, and how each is read.
#[pyclass(frozen, module = "rust.native.rows")]
pub struct ValuesReader {
    codecs: Vec<Py<FieldCodec>>,
}

impl ValuesReader {
    fn read_values<'py>(&self, row: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyTuple>> {
        let py = row.py();
        let mut values = Vec::with_capacity(self.codecs.len());
        for (index, codec) in self.codecs.iter().enumerate() {
            values.push(codec.get().read_value(ffi::get_row_item(row, index)?)?);
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

    /// A tuple of the column values of each row.
    fn read_tuples<'py>(&self, rows: &Bound<'py, PyList>) -> PyResult<Bound<'py, PyList>> {
        let mut tuples = ffi::ListBuilder::new(rows.py(), rows.len())?;
        for (index, row) in rows.iter().enumerate() {
            tuples.set(index, self.read_values(&row)?.into_any());
        }
        Ok(tuples.finish())
    }

    /// The value of the first column of each row.
    fn read_flat<'py>(&self, rows: &Bound<'py, PyList>) -> PyResult<Bound<'py, PyList>> {
        let py = rows.py();
        let codec = self.codecs.first().map(Py::get);
        let mut values = ffi::ListBuilder::new(py, rows.len())?;
        for (index, row) in rows.iter().enumerate() {
            let raw = ffi::get_row_item(&row, 0)?;
            values.set(
                index,
                match codec {
                    Some(codec) => codec.read_value(raw)?,
                    None => raw,
                },
            );
        }
        Ok(values.finish())
    }

    /// A dict of each row's column values under `keys`, one per column.
    fn read_dicts<'py>(
        &self,
        rows: &Bound<'py, PyList>,
        keys: Vec<Bound<'py, PyString>>,
    ) -> PyResult<Bound<'py, PyList>> {
        let py = rows.py();
        let mut dicts = ffi::ListBuilder::new(py, rows.len())?;
        for (index, row) in rows.iter().enumerate() {
            let dict = PyDict::new(py);
            for (column, (codec, key)) in self.codecs.iter().zip(&keys).enumerate() {
                dict.set_item(key, codec.get().read_value(ffi::get_row_item(&row, column)?)?)?;
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
        rows: &Bound<'_, PyList>,
        positions: Vec<usize>,
    ) -> PyResult<()> {
        let py = rows.py();
        for (instance, row) in instances.iter().zip(rows.iter()) {
            if instance.is_none() {
                continue;
            }
            for (codec, position) in self.codecs.iter().zip(&positions) {
                let codec = codec.get();
                let value = codec.read_value(ffi::get_row_item(&row, *position)?)?;
                ffi::generic_set_attribute(&instance, codec.name.bind(py), &value)?;
            }
        }
        Ok(())
    }

    /// An instance of `row_class`, a tuple subclass, holding each row's column values - built as
    /// `tuple.__new__(row_class, values)`, without the class's own `__new__`.
    fn read_named<'py>(
        &self,
        rows: &Bound<'py, PyList>,
        row_class: &Bound<'py, PyType>,
    ) -> PyResult<Bound<'py, PyList>> {
        let py = rows.py();
        let tuple_new = py.get_type::<PyTuple>().getattr(intern!(py, "__new__"))?;
        let mut named = ffi::ListBuilder::new(py, rows.len())?;
        for (index, row) in rows.iter().enumerate() {
            named.set(index, tuple_new.call1((row_class, self.read_values(&row)?))?);
        }
        Ok(named.finish())
    }
}
