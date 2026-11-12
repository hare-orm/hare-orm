//! How the value of a selected column is read.

use pyo3::prelude::*;
use pyo3::{PyTraverseError, PyVisit};

use crate::rows::json_column_reader::JsonColumnReader;
use crate::rows::typed_column_reader::TypedColumnReader;

/// The reader of a column, told apart once when its layout is made: the value as the driver gives it
/// (a reader of None), JSON text of a built-in `JSONField` (a `JsonColumnReader`), a value of a field
/// keeping its own type unchanged (a `TypedColumnReader`) - both read without a Python call where they
/// can - or a Python reader called with the value. It owns its reader, so a layout kept with a plan
/// keeps it too.
pub enum ColumnReader {
    AsIs,
    Json(Py<JsonColumnReader>),
    Typed(Py<TypedColumnReader>),
    Python(Py<PyAny>),
}

impl ColumnReader {
    pub fn new(reader: Bound<'_, PyAny>) -> Self {
        if reader.is_none() {
            return ColumnReader::AsIs;
        }
        let reader = match reader.cast_into::<JsonColumnReader>() {
            Ok(json_reader) => return ColumnReader::Json(json_reader.unbind()),
            Err(error) => error.into_inner(),
        };
        match reader.cast_into::<TypedColumnReader>() {
            Ok(typed_reader) => ColumnReader::Typed(typed_reader.unbind()),
            Err(error) => ColumnReader::Python(error.into_inner().unbind()),
        }
    }

    pub fn read<'py>(&self, raw: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        match self {
            ColumnReader::AsIs => Ok(raw),
            ColumnReader::Json(json_reader) => json_reader.get().read(raw),
            ColumnReader::Typed(typed_reader) => typed_reader.get().read(raw),
            ColumnReader::Python(reader) => reader.bind(raw.py()).call1((&raw,)),
        }
    }

    /// Visits the reader it holds - for the garbage collector of an object keeping it.
    pub fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        match self {
            ColumnReader::AsIs => Ok(()),
            ColumnReader::Json(json_reader) => visit.call(json_reader),
            ColumnReader::Typed(typed_reader) => visit.call(typed_reader),
            ColumnReader::Python(reader) => visit.call(reader),
        }
    }
}
