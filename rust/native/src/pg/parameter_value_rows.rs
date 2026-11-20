//! Parameter rows of `execute_many`, converted from Python sequences at the driver boundary.

use pyo3::prelude::*;
use pyo3::Borrowed;

use crate::pg::parameter_values::ParameterValues;
use crate::pg::value::Value;

pub struct ParameterValueRows(pub Vec<Vec<Value>>);

impl<'a, 'py> FromPyObject<'a, 'py> for ParameterValueRows {
    type Error = PyErr;

    fn extract(rows: Borrowed<'a, 'py, PyAny>) -> Result<Self, PyErr> {
        let rows = rows.extract::<Vec<Vec<Value>>>()?;
        for row in &rows {
            ParameterValues::check_count(row.len())?;
        }
        Ok(ParameterValueRows(rows))
    }
}
