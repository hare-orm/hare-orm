use crate::asyncio::future_into_py;
use crate::pg::connection_executor::PgConnectionExecutor;
use crate::pg::error::to_pyerr;
use crate::pg::parameter_values::ParameterValues;
use crate::pg::result::PgResult;
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
    fn execute<'py>(&self, py: Python<'py>, sql: String, parameters: ParameterValues) -> PyResult<Bound<'py, PyAny>> {
        let params = parameters.0;
        let executor = self.executor.clone();
        future_into_py(py, async move {
            let mut lease = executor.acquire().await.map_err(to_pyerr)?.ok_or_else(connection_released)?;
            lease.execute(&sql, &params, &executor.caching).await.map_err(to_pyerr)
        })
    }

    fn fetch_all<'py>(&self, py: Python<'py>, sql: String, parameters: ParameterValues) -> PyResult<Bound<'py, PyAny>> {
        let params = parameters.0;
        let executor = self.executor.clone();
        future_into_py(py, async move {
            let mut lease = executor.acquire().await.map_err(to_pyerr)?.ok_or_else(connection_released)?;
            lease
                .fetch(&sql, &params, &executor.caching)
                .await
                .map(|(columns, data)| PgResult::new(columns, data))
                .map_err(to_pyerr)
        })
    }
}
