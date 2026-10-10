//! `ResultColumns` - the column names of one result, shared by its rows and, for a stream, by every
//! batch of it.

use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::PyString;
use tokio_postgres::{Column, Statement};

use crate::pg::row::RowNames;

pub struct ResultColumns {
    names: Vec<String>,
    /// The names as Python strings - made on the first row read as a `PgRow`, on the event loop's
    /// thread.
    row_names: PyOnceLock<Arc<RowNames>>,
}

impl ResultColumns {
    /// The columns of `statement`'s rows.
    pub fn of_statement(statement: &Statement) -> Arc<Self> {
        Self::of_columns(statement.columns())
    }

    /// The result of rows with `columns`.
    pub fn of_columns(columns: &[Column]) -> Arc<Self> {
        Arc::new(ResultColumns {
            names: columns.iter().map(|column| column.name().to_string()).collect(),
            row_names: PyOnceLock::new(),
        })
    }

    /// No columns - the result of a stream already closed.
    pub fn none() -> Arc<Self> {
        Arc::new(ResultColumns { names: Vec::new(), row_names: PyOnceLock::new() })
    }

    pub fn names(&self) -> &[String] {
        &self.names
    }

    /// The names every `PgRow` of the result shares.
    pub fn get_row_names(&self, py: Python<'_>) -> &Arc<RowNames> {
        self.row_names.get_or_init(py, || {
            Arc::new(RowNames::new(self.names.iter().map(|name| PyString::new(py, name).unbind()).collect()))
        })
    }
}
