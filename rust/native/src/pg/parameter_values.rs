//! `ParameterValues` - the parameters a statement method takes: a `PgParameters`, or a sequence of
//! Python values, each read into a `Value`.

use pyo3::prelude::*;
use pyo3::Borrowed;

use crate::pg::parameters::PgParameters;
use crate::pg::value::Value;

pub struct ParameterValues(pub Vec<Value>);

impl<'a, 'py> FromPyObject<'a, 'py> for ParameterValues {
    type Error = PyErr;

    fn extract(parameters: Borrowed<'a, 'py, PyAny>) -> Result<Self, PyErr> {
        if let Ok(written) = parameters.cast::<PgParameters>() {
            return Ok(ParameterValues(written.get().get_values(parameters.py())?));
        }
        Ok(ParameterValues(parameters.extract::<Vec<Value>>()?))
    }
}
