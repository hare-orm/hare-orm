//! `FirstRow` - the one row `fetch_one()` and a stream's `__anext__()` return, made a `PgRow` on the
//! event loop's thread.

use std::sync::Arc;

use pyo3::prelude::*;

use crate::pg::result_columns::ResultColumns;
use crate::pg::row::PgRow;
use crate::pg::value::Value;

pub struct FirstRow {
    pub columns: Arc<ResultColumns>,
    /// The row's decoded values - None for a result without rows.
    pub values: Option<Vec<Value>>,
}

impl<'py> IntoPyObject<'py> for FirstRow {
    type Target = PyAny;
    type Output = Bound<'py, PyAny>;
    type Error = PyErr;

    /// The row's `PgRow`, None without a row.
    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        match self.values {
            Some(values) => {
                let row = PgRow::from_values(py, self.columns.get_row_names(py).clone(), &values)?;
                Ok(Bound::new(py, row)?.into_any())
            }
            None => Ok(py.None().into_bound(py)),
        }
    }
}
