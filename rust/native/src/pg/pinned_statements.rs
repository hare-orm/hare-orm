//! Python awaitables for single statements on a retained PostgreSQL connection.

use crate::asyncio::future_into_py;
use crate::pg::connection_executor::PgConnectionExecutor;
use crate::pg::error::to_pyerr;
use crate::pg::parameter_values::ParameterValues;
use crate::pg::result::PgResult;
use pyo3::prelude::*;

pub(super) fn execute<'py>(
    executor: &PgConnectionExecutor,
    py: Python<'py>,
    sql: String,
    parameters: ParameterValues,
    built_sql: bool,
    on_closed: fn() -> PyErr,
) -> PyResult<Bound<'py, PyAny>> {
    let params = parameters.0;
    let executor = executor.clone();
    future_into_py(py, async move {
        let mut lease = executor.acquire().await.map_err(to_pyerr)?.ok_or_else(on_closed)?;
        lease.execute(&sql, &params, &executor.caching, built_sql).await.map_err(to_pyerr)
    })
}

pub(super) fn fetch_all<'py>(
    executor: &PgConnectionExecutor,
    py: Python<'py>,
    sql: String,
    parameters: ParameterValues,
    built_sql: bool,
    on_closed: fn() -> PyErr,
) -> PyResult<Bound<'py, PyAny>> {
    let params = parameters.0;
    let executor = executor.clone();
    future_into_py(py, async move {
        let mut lease = executor.acquire().await.map_err(to_pyerr)?.ok_or_else(on_closed)?;
        lease
            .fetch(&sql, &params, &executor.caching, built_sql)
            .await
            .map(|(columns, data)| PgResult::new(columns, data))
            .map_err(to_pyerr)
    })
}
