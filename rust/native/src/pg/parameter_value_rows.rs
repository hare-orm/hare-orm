//! `ParameterValueRows` - the parameter rows `execute_many()` takes: a `PgParameterRows`, or a
//! sequence of sequences of Python values, each read into a `Value`.

use pyo3::prelude::*;
use pyo3::Borrowed;

use crate::pg::parameter_rows::PgParameterRows;
use crate::pg::value::Value;

pub struct ParameterValueRows(pub Vec<Vec<Value>>);

impl<'a, 'py> FromPyObject<'a, 'py> for ParameterValueRows {
    type Error = PyErr;

    fn extract(rows: Borrowed<'a, 'py, PyAny>) -> Result<Self, PyErr> {
        if let Ok(written) = rows.cast::<PgParameterRows>() {
            return Ok(ParameterValueRows(written.get().get_rows(rows.py())?));
        }
        Ok(ParameterValueRows(rows.extract::<Vec<Vec<Value>>>()?))
    }
}
