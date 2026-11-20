use crate::pg::connection_executor::PgConnectionExecutor;
use crate::pg::parameter_values::ParameterValues;
use crate::pg::pinned_statements;
use crate::pg::timed_manager::Object;
use crate::pg::tls::PgTls;

use super::{connection_released, CancelTarget, StatementCaching};
use pyo3::prelude::*;

/// A checked-out connection whose operations share the physical connection executor.
#[pyclass]
pub struct PooledConnection {
    pub(super) executor: PgConnectionExecutor,
}

impl PooledConnection {
    pub(super) fn new(client: Object, caching: StatementCaching, tls: PgTls, cancel_target: CancelTarget) -> Self {
        Self { executor: PgConnectionExecutor::new(client, caching, tls, cancel_target) }
    }
}

impl Drop for PooledConnection {
    fn drop(&mut self) {
        self.executor.release_in_background();
    }
}

#[pymethods]
impl PooledConnection {
    #[pyo3(signature = (sql, parameters, *, built_sql = false))]
    fn execute<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        parameters: ParameterValues,
        built_sql: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        pinned_statements::execute(&self.executor, py, sql, parameters, built_sql, connection_released)
    }

    #[pyo3(signature = (sql, parameters, *, built_sql = false))]
    fn fetch_all<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        parameters: ParameterValues,
        built_sql: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        pinned_statements::fetch_all(&self.executor, py, sql, parameters, built_sql, connection_released)
    }
}
