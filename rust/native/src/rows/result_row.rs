//! `ResultRow` - one row of `ResultRows`: a row of a `PgResult`, or a driver row object.

use pyo3::exceptions::PyIndexError;
use pyo3::prelude::*;

use crate::codecs::field_codec::FieldCodec;
use crate::pg::result::PgResult;
use crate::pg::result_cell::ResultCell;
use crate::python::ffi;

pub enum ResultRow<'a, 'py> {
    Decoded { py: Python<'py>, result: &'a PgResult, index: usize },
    Object(Bound<'py, PyAny>),
}

impl<'py> ResultRow<'_, 'py> {
    fn get_cell(result: &PgResult, index: usize, column: usize) -> PyResult<ResultCell<'_>> {
        result.get_cell(index, column).ok_or_else(|| PyIndexError::new_err("row index out of range"))
    }

    /// The attribute value of column `column`, read by `codec`.
    pub fn read(&self, column: usize, codec: &FieldCodec) -> PyResult<Bound<'py, PyAny>> {
        match self {
            ResultRow::Decoded { py, result, index } => codec.read_cell(*py, &Self::get_cell(result, *index, column)?),
            ResultRow::Object(row) => codec.read_value(ffi::get_row_item(row, column)?),
        }
    }

    /// The value of column `column` as the driver gives it, unread.
    pub fn get(&self, column: usize) -> PyResult<Bound<'py, PyAny>> {
        match self {
            ResultRow::Decoded { py, result, index } => Self::get_cell(result, *index, column)?.to_python(*py),
            ResultRow::Object(row) => ffi::get_row_item(row, column),
        }
    }

    /// Whether column `column` is NULL.
    pub fn is_null(&self, column: usize) -> PyResult<bool> {
        match self {
            ResultRow::Decoded { result, index, .. } => Ok(Self::get_cell(result, *index, column)?.is_null()),
            ResultRow::Object(row) => Ok(ffi::get_row_item(row, column)?.is_none()),
        }
    }
}
