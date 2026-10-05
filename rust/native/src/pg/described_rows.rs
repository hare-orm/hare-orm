//! `DescribedRows` - what `fetch_all_described()` returns: the result's column names, there for an
//! empty result too, and its rows.

use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::{PyList, PyTuple};

use crate::pg::result::PgResult;
use crate::pg::result_columns::ResultColumns;
use crate::pg::result_data::ResultData;

pub struct DescribedRows {
    pub columns: Arc<ResultColumns>,
    pub data: ResultData,
}

impl<'py> IntoPyObject<'py> for DescribedRows {
    type Target = PyTuple;
    type Output = Bound<'py, PyTuple>;
    type Error = PyErr;

    /// `(names, rows)` - the names a list of str, the rows a `PgResult`.
    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        let names = PyList::new(py, self.columns.names())?;
        let rows = Bound::new(py, PgResult::new(self.columns, self.data))?;
        PyTuple::new(py, [names.into_any(), rows.into_any()])
    }
}
