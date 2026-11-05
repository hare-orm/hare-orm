//! Parameter rows of `execute_many`, converted from Python sequences at the driver boundary.

use pyo3::prelude::*;
use pyo3::Borrowed;

use crate::pg::value::Value;

pub struct ParameterValueRows(pub Vec<Vec<Value>>);

impl<'a, 'py> FromPyObject<'a, 'py> for ParameterValueRows {
    type Error = PyErr;

    fn extract(rows: Borrowed<'a, 'py, PyAny>) -> Result<Self, PyErr> {
        Ok(ParameterValueRows(rows.extract::<Vec<Vec<Value>>>()?))
    }
}
