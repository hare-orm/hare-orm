//! `FirstRow` - the row a stream's `__anext__()` returns, made a `PgRow` on the
//! event loop's thread.

use std::sync::Arc;

use pyo3::prelude::*;

use crate::pg::result_columns::ResultColumns;
use crate::pg::row::PgRow;
use crate::pg::value::Value;

pub struct FirstRow {
    pub columns: Arc<ResultColumns>,
    pub values: Vec<Value>,
}

impl<'py> IntoPyObject<'py> for FirstRow {
    type Target = PgRow;
    type Output = Bound<'py, PgRow>;
    type Error = PyErr;

    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        let row = PgRow::from_values(py, self.columns.get_row_names(py).clone(), &self.values)?;
        Bound::new(py, row)
    }
}
