use crate::asyncio::completion::future_into_py;
use crate::pg::error::{to_pyerr, DriverError};
use crate::pg::first_row::FirstRow;
use crate::pg::one_round_trip::{binds_in_one_round_trip, get_result_columns, typed_parameters};
use crate::pg::parameter_values::ParameterValues;
use crate::pg::result::PgResult;
use crate::pg::result_columns::ResultColumns;
use crate::pg::result_data::ResultData;
use crate::pg::timed_manager::Object;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use tokio::sync::Mutex;

use super::{
    as_sql_params, connection_released, decode_rows, prepare_and_run_with_stale_plan_retry, CancelQueryOnDrop,
    CancelTarget, PendingCancels, PgTls, StatementCaching,
};
use pyo3::prelude::*;

/// One plain pooled connection from `Client::acquire()`. The connection sits in an
/// `Arc<Mutex<Option<Object>>>` (an async method can't borrow `&self` into its future), and it uses
/// the pool's statement caching setting. A query abandoned by the Python side is cancelled on the
/// server, as the pool's own queries are.
#[pyclass]
pub struct PooledConnection {
    pub(super) inner: Arc<Mutex<Option<Object>>>,
    statement_caching: StatementCaching,
    /// Set once a query failed because the connection itself is gone, or the cancel of an abandoned
    /// query couldn't be delivered - it is then closed instead of going back to the pool.
    pub(super) connection_lost: Arc<AtomicBool>,
    tls: PgTls,
    cancel_target: CancelTarget,
    /// The cancels of abandoned queries still on their way - the next statement and the release wait
    /// for them.
    pub(super) pending_cancels: PendingCancels,
}

impl PooledConnection {
    pub(super) fn new(
        client: Object,
        statement_caching: StatementCaching,
        tls: PgTls,
        cancel_target: CancelTarget,
    ) -> Self {
        Self {
            inner: Arc::new(Mutex::new(Some(client))),
            statement_caching,
            connection_lost: Arc::new(AtomicBool::new(false)),
            tls,
            cancel_target,
            pending_cancels: PendingCancels::default(),
        }
    }

    /// Returns the connection to the pool, or closes it when a query found it dead.
    pub(super) fn release_connection(connection: Object, connection_lost: &AtomicBool) {
        if connection_lost.load(Ordering::Acquire) {
            drop(Object::take(connection));
        } else {
            drop(connection);
        }
    }

    /// Passes a query's outcome through, remembering when it failed because the connection is gone.
    fn note_outcome<T>(connection_lost: &AtomicBool, outcome: Result<T, DriverError>) -> PyResult<T> {
        outcome.map_err(|error| {
            if error.is_connection_lost() {
                connection_lost.store(true, Ordering::Release);
            }
            to_pyerr(error)
        })
    }
}

impl Drop for PooledConnection {
    fn drop(&mut self) {
        if let Ok(mut guard) = self.inner.try_lock() {
            if let Some(connection) = guard.take() {
                if !self.pending_cancels.is_empty() {
                    // Dropped with a CancelRequest still on its way - closed, never handed to another
                    // caller whose query the late cancel could stop.
                    self.connection_lost.store(true, Ordering::Release);
                }
                PooledConnection::release_connection(connection, &self.connection_lost);
            }
        }
    }
}

#[pymethods]
impl PooledConnection {
    fn execute<'py>(&self, py: Python<'py>, sql: String, parameters: ParameterValues) -> PyResult<Bound<'py, PyAny>> {
        let params = parameters.0;
        let inner = self.inner.clone();
        let caching = self.statement_caching.clone();
        let connection_lost = self.connection_lost.clone();
        let tls = self.tls.clone();
        let cancel_target = self.cancel_target.clone();
        let pending_cancels = self.pending_cancels.clone();
        future_into_py(py, async move {
            // A cancel of an abandoned query still on its way could stop this one.
            pending_cancels.wait_until_delivered().await;
            let guard = inner.lock().await;
            let client = guard.as_ref().ok_or_else(connection_released)?;
            let mut cancel_guard = CancelQueryOnDrop::for_held_connection(
                client.cancel_token(),
                tls,
                cancel_target,
                connection_lost.clone(),
                pending_cancels,
            );
            let bound = as_sql_params(&params);
            let outcome = if binds_in_one_round_trip(&caching, &params) {
                client.execute_typed(&sql, &typed_parameters(&params)).await.map_err(DriverError::from)
            } else {
                prepare_and_run_with_stale_plan_retry(client, &sql, &params, &caching, |statement| {
                    let bound = &bound;
                    async move { client.execute(&statement, bound).await }
                })
                .await
                .map(|(_, affected)| affected)
            };
            let affected = PooledConnection::note_outcome(&connection_lost, cancel_guard.finish(outcome))?;
            Ok(affected)
        })
    }

    fn fetch_all<'py>(&self, py: Python<'py>, sql: String, parameters: ParameterValues) -> PyResult<Bound<'py, PyAny>> {
        let params = parameters.0;
        let inner = self.inner.clone();
        let caching = self.statement_caching.clone();
        let connection_lost = self.connection_lost.clone();
        let tls = self.tls.clone();
        let cancel_target = self.cancel_target.clone();
        let pending_cancels = self.pending_cancels.clone();
        future_into_py(py, async move {
            // A cancel of an abandoned query still on its way could stop this one.
            pending_cancels.wait_until_delivered().await;
            let guard = inner.lock().await;
            let client = guard.as_ref().ok_or_else(connection_released)?;
            let mut cancel_guard = CancelQueryOnDrop::for_held_connection(
                client.cancel_token(),
                tls,
                cancel_target,
                connection_lost.clone(),
                pending_cancels,
            );
            let bound = as_sql_params(&params);
            let outcome = async {
                if binds_in_one_round_trip(&caching, &params) {
                    let rows = client.query_typed(&sql, &typed_parameters(&params)).await?;
                    let columns = get_result_columns(client, &sql, &params, &rows).await?;
                    return Ok((columns, ResultData::read(client, rows).await?));
                }
                let (statement, rows) =
                    prepare_and_run_with_stale_plan_retry(client, &sql, &params, &caching, |statement| {
                        let bound = &bound;
                        async move { client.query(&statement, bound).await }
                    })
                    .await?;
                Ok((ResultColumns::of_statement(&statement), ResultData::read(client, rows).await?))
            }
            .await;
            let (columns, rows) = PooledConnection::note_outcome(&connection_lost, cancel_guard.finish(outcome))?;
            Ok(PgResult::new(columns, rows))
        })
    }

    fn fetch_one<'py>(&self, py: Python<'py>, sql: String, parameters: ParameterValues) -> PyResult<Bound<'py, PyAny>> {
        let params = parameters.0;
        let inner = self.inner.clone();
        let caching = self.statement_caching.clone();
        let connection_lost = self.connection_lost.clone();
        let tls = self.tls.clone();
        let cancel_target = self.cancel_target.clone();
        let pending_cancels = self.pending_cancels.clone();
        future_into_py(py, async move {
            // A cancel of an abandoned query still on its way could stop this one.
            pending_cancels.wait_until_delivered().await;
            let guard = inner.lock().await;
            let client = guard.as_ref().ok_or_else(connection_released)?;
            let mut cancel_guard = CancelQueryOnDrop::for_held_connection(
                client.cancel_token(),
                tls,
                cancel_target,
                connection_lost.clone(),
                pending_cancels,
            );
            let bound = as_sql_params(&params);
            let outcome = async {
                if binds_in_one_round_trip(&caching, &params) {
                    let rows = client.query_typed(&sql, &typed_parameters(&params)).await?;
                    let columns = get_result_columns(client, &sql, &params, &rows).await?;
                    return Ok((columns, decode_rows(client, &rows[..rows.len().min(1)]).await?));
                }
                let (statement, rows) =
                    prepare_and_run_with_stale_plan_retry(client, &sql, &params, &caching, |statement| {
                        let bound = &bound;
                        async move { client.query(&statement, bound).await }
                    })
                    .await?;
                Ok((ResultColumns::of_statement(&statement), decode_rows(client, &rows[..rows.len().min(1)]).await?))
            }
            .await;
            let (columns, rows) = PooledConnection::note_outcome(&connection_lost, cancel_guard.finish(outcome))?;
            Ok(FirstRow { columns, values: rows.into_iter().next() })
        })
    }
}
