//! `Transaction` - a pinned-connection PostgreSQL transaction, PyO3-exposed. The connection
//! lives in an `Arc<Mutex<Option<Object>>>` (an async pymethod can't borrow `&self` into a
//! `'static` future, so every method clones the `Arc` to reach it), and a drop guard is the
//! safety net: a transaction dropped without commit/rollback - e.g. the owning coroutine was
//! cancelled - rolls back on the background tokio runtime rather than letting deadpool's
//! `RecyclingMethod::Fast` recycle a still-BEGIN'd connection into the pool, which would corrupt
//! the next consumer's session. The design follows yara-orm's `Transaction`/`PgTx`
//! (<https://github.com/vsdudakov/yara-orm>); the code is hare-orm's own.
//!
//! Savepoints (`SAVEPOINT`/`RELEASE SAVEPOINT`/`ROLLBACK TO SAVEPOINT`) are plain SQL sent via
//! `batch_execute` - tokio-postgres has no dedicated savepoint API, and none is needed. Naming
//! and nesting-depth bookkeeping (which wrapper owns the transaction vs. which is a nested
//! savepoint reusing it) is entirely the Python side's responsibility
//! (the `rust_pg` driver's own `RustPgTransactionClient`) - this type only executes the
//! three savepoint SQL forms against whatever name it's given.
//!
//! `BEGIN` is not sent when the transaction starts: it waits for the transaction's first
//! statement and goes out with it in one round trip (pipelined ahead of an extended-protocol
//! statement, or prepended to a simple-protocol one). A transaction that runs no statement never
//! reaches the server at all - its COMMIT/ROLLBACK send nothing. PostgreSQL takes a transaction's
//! snapshot at its first statement, not at BEGIN, so nothing a statement sees changes.

use std::collections::VecDeque;
use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex as StdMutex, Weak};
use std::time::Duration;

use crate::pg::completion::future_into_py;
use crate::pg::row::{PgRow, RowNames};
use deadpool_postgres::Object;
use futures_util::StreamExt;
use pyo3::prelude::*;
use pyo3::types::PyList;
use tokio::sync::Mutex;

use crate::pg::client::{
    as_sql_params, decode_rows, described_rows, prepare_for, row_names, rows_to_pgrows, unified_param_types,
    CancelQueryOnDrop, CancelTarget, PendingCancels, PgTls, PinnedConnectionState, StatementCaching,
};
use crate::pg::error::to_pyerr;
use crate::pg::error::ConnectionClosedError;
use crate::pg::error::DriverError;
use crate::pg::error::TransactionFinishedError;
use crate::pg::value::{decode_pg_row_values, type_may_need_server_text_form, ServerTextForms, Value};

/// How long a ROLLBACK (explicit or the drop safety net's) may take before the connection is
/// closed instead - closing it aborts the transaction server-side, so the rollback still happens,
/// and a connection whose ROLLBACK never answers is never handed back to the pool.
const ROLLBACK_TIMEOUT: Duration = Duration::from_secs(30);

fn tx_finished() -> PyErr {
    TransactionFinishedError::new_err("transaction already committed or rolled back")
}

fn connection_closed_before(statement: &str) -> PyErr {
    ConnectionClosedError::new_err(format!("the connection was closed before {statement} could be sent"))
}

/// Checks a savepoint name: it is spliced into the SQL text (an identifier has no bind parameter).
fn validate_identifier(name: &str) -> PyResult<()> {
    let valid = !name.is_empty()
        && name.len() <= 63
        && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
        && !name.chars().next().unwrap().is_ascii_digit();
    if valid {
        Ok(())
    } else {
        Err(pyo3::exceptions::PyValueError::new_err(format!("invalid savepoint name: {name:?}")))
    }
}

/// The canonical spelling of an isolation level (checked case-insensitively against PostgreSQL's
/// four), spliced into `BEGIN ISOLATION LEVEL` - which has no bind parameter either.
fn validate_isolation_level(level: &str) -> Result<&'static str, DriverError> {
    match level.to_ascii_uppercase().as_str() {
        "READ UNCOMMITTED" => Ok("READ UNCOMMITTED"),
        "READ COMMITTED" => Ok("READ COMMITTED"),
        "REPEATABLE READ" => Ok("REPEATABLE READ"),
        "SERIALIZABLE" => Ok("SERIALIZABLE"),
        _ => Err(DriverError::Config(format!("invalid isolation level: {level:?}"))),
    }
}

#[pyclass]
pub struct Transaction {
    inner: Arc<Mutex<Option<Object>>>,
    statement_caching: StatementCaching,
    /// Shared with every query guard on the pinned connection: whether it may go back to the
    /// pool, and the `CancelRequests` of abandoned statements later statements wait for.
    pinned: PinnedConnectionState,
    /// Every `RowStreamIterator` opened on this transaction - ending the transaction closes them.
    open_streams: Arc<StdMutex<Vec<Weak<RowStreamSlot>>>>,
    /// The BEGIN not sent yet - the transaction's first statement sends it (see
    /// `after_pending_begin`). None once it was sent.
    pending_begin: Arc<StdMutex<Option<String>>>,
}

impl Transaction {
    pub(crate) fn begin(
        client: Object,
        isolation: Option<&str>,
        statement_caching: StatementCaching,
        tls: PgTls,
        cancel_target: CancelTarget,
    ) -> Result<Self, DriverError> {
        let begin_sql = match isolation {
            Some(level) => format!("BEGIN ISOLATION LEVEL {}", validate_isolation_level(level)?),
            None => "BEGIN".to_string(),
        };
        Ok(Transaction {
            inner: Arc::new(Mutex::new(Some(client))),
            statement_caching,
            pinned: PinnedConnectionState {
                tls,
                cancel_target,
                connection_poisoned: Arc::new(AtomicBool::new(false)),
                pending_cancels: PendingCancels::default(),
            },
            open_streams: Arc::new(StdMutex::new(Vec::new())),
            pending_begin: Arc::new(StdMutex::new(Some(begin_sql))),
        })
    }

    /// Takes the BEGIN still to be sent, if any - whoever takes it sends it.
    fn take_pending_begin(pending_begin: &StdMutex<Option<String>>) -> Option<String> {
        pending_begin.lock().unwrap_or_else(std::sync::PoisonError::into_inner).take()
    }

    /// Runs `statement` - a future that sends one statement when first polled - after the
    /// transaction's BEGIN when that is still to be sent, in the same round trip: both requests are
    /// queued on the connection in that order within one poll, so the server runs BEGIN first and
    /// answers both at once.
    ///
    /// The BEGIN is taken only here, right before both are sent: a statement abandoned while it
    /// was still preparing leaves it for the next statement.
    async fn after_pending_begin<T>(
        client: &Object,
        pending_begin: &StdMutex<Option<String>>,
        statement: impl Future<Output = Result<T, DriverError>>,
    ) -> Result<T, DriverError> {
        match Transaction::take_pending_begin(pending_begin) {
            None => statement.await,
            Some(begin_sql) => {
                let (begun, result) = futures_util::future::join(client.batch_execute(&begin_sql), statement).await;
                begun.map_err(DriverError::from)?;
                result
            }
        }
    }

    /// Closes every still-open `RowStreamIterator` of this transaction, dropping its portal.
    ///
    /// An unread portal fills its response channel, which stops tokio-postgres's connection task
    /// from reading anything else off the socket - a COMMIT/ROLLBACK sent while one is still open
    /// would never see its answer.
    fn close_open_streams(open_streams: &StdMutex<Vec<Weak<RowStreamSlot>>>) {
        let slots = std::mem::take(&mut *open_streams.lock().unwrap_or_else(std::sync::PoisonError::into_inner));
        for slot in slots.iter().filter_map(Weak::upgrade) {
            slot.close(true);
        }
    }

    /// Registers a newly opened stream, forgetting the ones already gone.
    fn register_stream(open_streams: &StdMutex<Vec<Weak<RowStreamSlot>>>, slot: &Arc<RowStreamSlot>) {
        let mut registered = open_streams.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
        registered.retain(|weak| weak.strong_count() > 0);
        registered.push(Arc::downgrade(slot));
    }

    /// Sends ROLLBACK, bounded by `ROLLBACK_TIMEOUT`, then releases the connection.
    async fn roll_back_and_release(client: Object, connection_poisoned: &AtomicBool) -> Result<(), DriverError> {
        let outcome = match tokio::time::timeout(ROLLBACK_TIMEOUT, client.batch_execute("ROLLBACK")).await {
            Ok(outcome) => outcome.map_err(DriverError::from),
            Err(_) => Err(DriverError::Connection(format!(
                "ROLLBACK got no answer within {}s - the connection was closed, which aborts the transaction",
                ROLLBACK_TIMEOUT.as_secs()
            ))),
        };
        if matches!(&outcome, Err(error) if error.is_connection_lost()) {
            connection_poisoned.store(true, Ordering::Release);
        }
        Transaction::release_connection(client, connection_poisoned);
        outcome
    }

    /// Hands the pinned connection back to the pool - or closes it when a `CancelRequest` sent for
    /// an abandoned query on it may still arrive (it could cancel the next caller's query) or the
    /// connection turned out dead.
    fn release_connection(connection: Object, connection_poisoned: &AtomicBool) {
        if connection_poisoned.load(Ordering::Acquire) {
            drop(Object::take(connection));
        } else {
            drop(connection);
        }
    }

    /// Passes an outcome through as a Python result, poisoning the connection when the error
    /// means the connection itself is gone.
    fn note_outcome<T>(connection_poisoned: &AtomicBool, outcome: Result<T, DriverError>) -> PyResult<T> {
        outcome.map_err(|error| {
            if error.is_connection_lost() {
                connection_poisoned.store(true, Ordering::Release);
            }
            to_pyerr(error)
        })
    }
}

impl Drop for Transaction {
    fn drop(&mut self) {
        let inner = self.inner.clone();
        let pinned = self.pinned.clone();
        let pending_begin = self.pending_begin.clone();
        Transaction::close_open_streams(&self.open_streams);
        pyo3_async_runtimes::tokio::get_runtime().spawn(async move {
            if let Some(client) = inner.lock().await.take() {
                pinned.pending_cancels.wait_until_delivered().await;
                if pinned.connection_poisoned.load(Ordering::Acquire)
                    || Transaction::take_pending_begin(&pending_begin).is_some()
                {
                    // Closing the connection aborts the transaction server-side; a transaction
                    // whose BEGIN was never sent has nothing to roll back.
                    Transaction::release_connection(client, &pinned.connection_poisoned);
                    return;
                }
                // The abandoned-mid-transaction safety net - commit()/rollback() take the
                // connection themselves before this ever runs.
                let _ = Transaction::roll_back_and_release(client, &pinned.connection_poisoned).await;
            }
        });
    }
}

#[pymethods]
impl Transaction {
    fn execute<'py>(&self, py: Python<'py>, sql: String, params: Vec<Value>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        let caching = self.statement_caching.clone();
        let pinned = self.pinned.clone();
        let pending_begin = self.pending_begin.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let client = guard.as_ref().ok_or_else(tx_finished)?;
            pinned.pending_cancels.wait_until_delivered().await;
            let stmt = Transaction::note_outcome(
                &pinned.connection_poisoned,
                prepare_for(client, &sql, &params, &caching).await,
            )?;
            let bound = as_sql_params(&params);
            // Constructed only now, right before the one await it guards - a PREPARE failure is an
            // ordinary immediate server response with nothing in flight to cancel. See
            // Client::execute's own guard for the "cancelling the Python await doesn't stop the
            // query server-side" gap this closes.
            let mut cancel_guard = CancelQueryOnDrop::for_pinned_connection(client.cancel_token(), &pinned);
            let result = Transaction::after_pending_begin(client, &pending_begin, async {
                client.execute(&stmt, &bound).await.map_err(DriverError::from)
            })
            .await;
            cancel_guard.finish(result).map_err(to_pyerr)
        })
    }

    fn fetch_all<'py>(&self, py: Python<'py>, sql: String, params: Vec<Value>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        let caching = self.statement_caching.clone();
        let pinned = self.pinned.clone();
        let pending_begin = self.pending_begin.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let client = guard.as_ref().ok_or_else(tx_finished)?;
            pinned.pending_cancels.wait_until_delivered().await;
            let stmt = Transaction::note_outcome(
                &pinned.connection_poisoned,
                prepare_for(client, &sql, &params, &caching).await,
            )?;
            let bound = as_sql_params(&params);
            // See execute()'s own identical guard just above.
            let mut cancel_guard = CancelQueryOnDrop::for_pinned_connection(client.cancel_token(), &pinned);
            let result = Transaction::after_pending_begin(client, &pending_begin, async {
                let rows = client.query(&stmt, &bound).await?;
                decode_rows(client, &rows).await
            })
            .await;
            let rows = cancel_guard.finish(result).map_err(to_pyerr)?;
            Python::attach(|py| {
                let names = row_names(py, &stmt);
                let pgrows = rows_to_pgrows(py, rows, &names)?;
                Ok(PyList::new(py, pgrows)?.unbind())
            })
        })
    }

    /// `fetch_all()` with the statement's column names first - `(names, rows)`, the names there for
    /// an empty result too.
    fn fetch_all_described<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        params: Vec<Value>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        let caching = self.statement_caching.clone();
        let pinned = self.pinned.clone();
        let pending_begin = self.pending_begin.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let client = guard.as_ref().ok_or_else(tx_finished)?;
            pinned.pending_cancels.wait_until_delivered().await;
            let stmt = Transaction::note_outcome(
                &pinned.connection_poisoned,
                prepare_for(client, &sql, &params, &caching).await,
            )?;
            let bound = as_sql_params(&params);
            // See execute()'s own identical guard.
            let mut cancel_guard = CancelQueryOnDrop::for_pinned_connection(client.cancel_token(), &pinned);
            let result = Transaction::after_pending_begin(client, &pending_begin, async {
                let rows = client.query(&stmt, &bound).await?;
                decode_rows(client, &rows).await
            })
            .await;
            let rows = cancel_guard.finish(result).map_err(to_pyerr)?;
            Python::attach(|py| described_rows(py, &stmt, rows))
        })
    }

    fn fetch_one<'py>(&self, py: Python<'py>, sql: String, params: Vec<Value>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        let caching = self.statement_caching.clone();
        let pinned = self.pinned.clone();
        let pending_begin = self.pending_begin.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let client = guard.as_ref().ok_or_else(tx_finished)?;
            pinned.pending_cancels.wait_until_delivered().await;
            let stmt = Transaction::note_outcome(
                &pinned.connection_poisoned,
                prepare_for(client, &sql, &params, &caching).await,
            )?;
            let bound = as_sql_params(&params);
            // See execute()'s own identical guard above.
            let mut cancel_guard = CancelQueryOnDrop::for_pinned_connection(client.cancel_token(), &pinned);
            let result = Transaction::after_pending_begin(client, &pending_begin, async {
                let rows = client.query(&stmt, &bound).await?;
                decode_rows(client, &rows[..rows.len().min(1)]).await
            })
            .await;
            let rows = cancel_guard.finish(result).map_err(to_pyerr)?;
            Python::attach(|py| {
                if rows.is_empty() {
                    Ok(py.None())
                } else {
                    let names = row_names(py, &stmt);
                    let pgrows = rows_to_pgrows(py, rows, &names)?;
                    Ok(Py::new(py, pgrows.into_iter().next().expect("exactly one row was decoded"))?.into_any())
                }
            })
        })
    }

    /// `QuerySet.stream()`'s `rust_pg` execution primitive - issues `sql`/`params` via
    /// `query_raw()` (rows arrive incrementally off the wire as the caller consumes them) against
    /// this transaction's own pinned connection, and hands back a `RowStreamIterator` bound to it.
    ///
    /// Holds the `inner` lock only long enough to prepare the statement and issue `query_raw` -
    /// `tokio_postgres::RowStream` borrows nothing from the connection, so a query issued on this
    /// same transaction between two `__anext__()` calls can acquire the lock immediately.
    ///
    /// When a result column has a type whose values need the server's text form (see
    /// `ServerTextForms`), the rows are fetched and converted up front instead: a conversion query
    /// cannot run on the connection while the portal is still being read.
    ///
    /// The stream is registered with this transaction: ending the transaction (commit, rollback,
    /// drop) closes it first, and its later `__anext__()` calls raise `TransactionFinishedError`.
    fn stream<'py>(&self, py: Python<'py>, sql: String, params: Vec<Value>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        let caching = self.statement_caching.clone();
        let pinned = self.pinned.clone();
        let open_streams = self.open_streams.clone();
        let pending_begin = self.pending_begin.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let client = guard.as_ref().ok_or_else(tx_finished)?;
            pinned.pending_cancels.wait_until_delivered().await;
            let connection_poisoned = pinned.connection_poisoned;
            let stmt =
                Transaction::note_outcome(&connection_poisoned, prepare_for(client, &sql, &params, &caching).await)?;
            let names = Python::attach(|py| row_names(py, &stmt));
            let bound = as_sql_params(&params);
            let source = if stmt.columns().iter().any(|column| type_may_need_server_text_form(column.type_())) {
                let outcome = Transaction::after_pending_begin(client, &pending_begin, async {
                    let rows = client.query(&stmt, &bound).await?;
                    decode_rows(client, &rows).await
                })
                .await;
                RowSource::Buffered(Transaction::note_outcome(&connection_poisoned, outcome)?.into())
            } else {
                let outcome = Transaction::after_pending_begin(client, &pending_begin, async {
                    client.query_raw(&stmt, bound).await.map_err(DriverError::from)
                })
                .await;
                RowSource::Live(Box::pin(Transaction::note_outcome(&connection_poisoned, outcome)?))
            };
            let slot = Arc::new(RowStreamSlot {
                state: Mutex::new(Some(OpenRowStream { source, names, connection_poisoned, pending_error: None })),
                close_requested: AtomicBool::new(false),
                closed_by_transaction_end: AtomicBool::new(false),
            });
            // Registered while the connection lock is still held - commit()/rollback() close the
            // registered streams again once they hold that lock, so none can slip past them.
            Transaction::register_stream(&open_streams, &slot);
            drop(guard);
            Python::attach(|py| -> PyResult<Py<PyAny>> { Ok(Py::new(py, RowStreamIterator { slot })?.into_any()) })
        })
    }

    /// Pipelined batch execute against the ALREADY-OPEN transaction - no nested BEGIN/COMMIT the
    /// way `Client::execute_many` wraps a fresh connection (the batch is already inside the
    /// caller's transaction). One `prepare`, then every row's `execute_raw()` fired without
    /// awaiting each individually, so `bulk_create/bulk_update` keep one round trip per batch
    /// inside `Transactions.atomic()` too.
    fn execute_many<'py>(&self, py: Python<'py>, sql: String, rows: Vec<Vec<Value>>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        let caching = self.statement_caching.clone();
        let pinned = self.pinned.clone();
        let pending_begin = self.pending_begin.clone();
        future_into_py(py, async move {
            if rows.is_empty() {
                return Ok(());
            }
            let types = unified_param_types(&rows).map_err(to_pyerr)?;
            let guard = inner.lock().await;
            let client = guard.as_ref().ok_or_else(tx_finished)?;
            pinned.pending_cancels.wait_until_delivered().await;
            let outcome: Result<(), DriverError> = async {
                let stmt = caching.prepare(client, &sql, &types).await?;
                Transaction::after_pending_begin(client, &pending_begin, async {
                    let futures = rows.iter().map(|row| client.execute_raw(&stmt, row.iter()));
                    futures_util::future::try_join_all(futures).await?;
                    Ok(())
                })
                .await
            }
            .await;
            Transaction::note_outcome(&pinned.connection_poisoned, outcome)
        })
    }

    /// Runs a (possibly multi-statement) script on this transaction's own pinned connection - a
    /// `TRUNCATE` from a second pool connection would block forever on the lock this transaction
    /// already holds.
    fn execute_script<'py>(&self, py: Python<'py>, sql: String) -> PyResult<Bound<'py, PyAny>> {
        self.run_on_connection(py, sql)
    }

    /// Ends the transaction right away when its BEGIN was never sent - no statement ran, so the
    /// server never saw it - giving the connection back to the pool without a trip to the tokio
    /// runtime. False when a statement ran or is still running: `commit()`/`rollback()` end it.
    fn end_unbegun(&self) -> bool {
        let Ok(mut guard) = self.inner.try_lock() else {
            return false;
        };
        if guard.is_none() || !self.pinned.pending_cancels.is_empty() {
            return false;
        }
        if Transaction::take_pending_begin(&self.pending_begin).is_none() {
            return false;
        }
        if let Some(client) = guard.take() {
            Transaction::release_connection(client, &self.pinned.connection_poisoned);
        }
        true
    }

    fn commit<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        let pinned = self.pinned.clone();
        let open_streams = self.open_streams.clone();
        // Before waiting for the connection lock too: a stream() still waiting on the connection
        // behind an unread portal holds that lock.
        Transaction::close_open_streams(&open_streams);
        let pending_begin = self.pending_begin.clone();
        future_into_py(py, async move {
            let client = inner.lock().await.take().ok_or_else(tx_finished)?;
            Transaction::close_open_streams(&open_streams);
            pinned.pending_cancels.wait_until_delivered().await;
            if Transaction::take_pending_begin(&pending_begin).is_some() {
                // No statement ran - the server never saw the transaction.
                Transaction::release_connection(client, &pinned.connection_poisoned);
                return Ok(());
            }
            if client.is_closed() {
                // Certainly never reached the server - unlike an error while the COMMIT is in
                // flight, which leaves its outcome unknown.
                pinned.connection_poisoned.store(true, Ordering::Release);
                Transaction::release_connection(client, &pinned.connection_poisoned);
                return Err(connection_closed_before("COMMIT"));
            }
            let outcome = client.batch_execute("COMMIT").await.map_err(DriverError::from);
            let result = Transaction::note_outcome(&pinned.connection_poisoned, outcome);
            Transaction::release_connection(client, &pinned.connection_poisoned);
            result
        })
    }

    /// Releases the pinned connection back to the pool WITHOUT sending any SQL - for after
    /// `PREPARE TRANSACTION`, which already ended the SQL-level transaction block itself (a COMMIT
    /// or ROLLBACK would error with "no transaction in progress").
    fn finish_prepared<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        let pinned = self.pinned.clone();
        let open_streams = self.open_streams.clone();
        Transaction::close_open_streams(&open_streams);
        future_into_py(py, async move {
            let connection = inner.lock().await.take().ok_or_else(tx_finished)?;
            Transaction::close_open_streams(&open_streams);
            pinned.pending_cancels.wait_until_delivered().await;
            Transaction::release_connection(connection, &pinned.connection_poisoned);
            Ok(())
        })
    }

    /// Bounded by `ROLLBACK_TIMEOUT`: past it the connection is closed (aborting the transaction
    /// server-side) and `ConnectionError` is raised.
    fn rollback<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        let pinned = self.pinned.clone();
        let open_streams = self.open_streams.clone();
        // See commit() for why this also runs before the lock is taken.
        Transaction::close_open_streams(&open_streams);
        let pending_begin = self.pending_begin.clone();
        future_into_py(py, async move {
            let client = inner.lock().await.take().ok_or_else(tx_finished)?;
            Transaction::close_open_streams(&open_streams);
            pinned.pending_cancels.wait_until_delivered().await;
            if Transaction::take_pending_begin(&pending_begin).is_some() {
                // No statement ran - the server never saw the transaction.
                Transaction::release_connection(client, &pinned.connection_poisoned);
                return Ok(());
            }
            if pinned.connection_poisoned.load(Ordering::Acquire) {
                // Closing the connection aborts the transaction server-side - no ROLLBACK for a
                // CancelRequest whose delivery was never confirmed to hit.
                Transaction::release_connection(client, &pinned.connection_poisoned);
                return Ok(());
            }
            if client.is_closed() {
                pinned.connection_poisoned.store(true, Ordering::Release);
                Transaction::release_connection(client, &pinned.connection_poisoned);
                return Err(connection_closed_before("ROLLBACK"));
            }
            Transaction::roll_back_and_release(client, &pinned.connection_poisoned).await.map_err(to_pyerr)
        })
    }

    fn savepoint<'py>(&self, py: Python<'py>, name: String) -> PyResult<Bound<'py, PyAny>> {
        validate_identifier(&name)?;
        self.run_on_connection(py, format!("SAVEPOINT {name}"))
    }

    fn release<'py>(&self, py: Python<'py>, name: String) -> PyResult<Bound<'py, PyAny>> {
        validate_identifier(&name)?;
        self.run_on_connection(py, format!("RELEASE SAVEPOINT {name}"))
    }

    fn rollback_to<'py>(&self, py: Python<'py>, name: String) -> PyResult<Bound<'py, PyAny>> {
        validate_identifier(&name)?;
        self.run_on_connection(py, format!("ROLLBACK TO SAVEPOINT {name}"))
    }
}

impl Transaction {
    /// Runs one simple-protocol statement (a savepoint statement, a script) on the pinned
    /// connection - after the transaction's BEGIN in the same query text when that is still to be
    /// sent (an explicit BEGIN keeps the transaction open past the end of the query).
    fn run_on_connection<'py>(&self, py: Python<'py>, sql: String) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        let pinned = self.pinned.clone();
        let pending_begin = self.pending_begin.clone();
        future_into_py(py, async move {
            let guard = inner.lock().await;
            let client = guard.as_ref().ok_or_else(tx_finished)?;
            pinned.pending_cancels.wait_until_delivered().await;
            // Taken right before the query is sent - batch_execute() sends it when first polled.
            let sql = match Transaction::take_pending_begin(&pending_begin) {
                Some(begin_sql) => format!("{begin_sql}; {sql}"),
                None => sql,
            };
            Transaction::note_outcome(
                &pinned.connection_poisoned,
                client.batch_execute(&sql).await.map_err(DriverError::from),
            )
        })
    }
}

/// Where a `RowStreamIterator`'s rows come from: the live portal, or rows fetched and decoded up
/// front (see `Transaction::stream`). The live stream is boxed and pinned once so
/// `StreamExt::next()` (which needs `Unpin`) can be called on it repeatedly.
enum RowSource {
    Live(Pin<Box<tokio_postgres::RowStream>>),
    Buffered(VecDeque<Vec<Value>>),
}

/// State backing one open `RowStreamIterator`.
struct OpenRowStream {
    source: RowSource,
    names: Arc<RowNames>,
    connection_poisoned: Arc<AtomicBool>,
    /// An error met after rows already returned by `fetch_many()` - raised by the next fetch.
    pending_error: Option<DriverError>,
}

impl OpenRowStream {
    /// The next row's values, None at the end of the stream.
    async fn next_values(&mut self) -> Option<Result<Vec<Value>, DriverError>> {
        if let Some(error) = self.pending_error.take() {
            return Some(Err(error));
        }
        match &mut self.source {
            RowSource::Buffered(rows) => rows.pop_front().map(Ok),
            RowSource::Live(stream) => match stream.next().await {
                Some(Ok(row)) => Some({
                    let mut forms = ServerTextForms::collecting();
                    decode_pg_row_values(&row, &mut forms).and_then(|values| {
                        if forms.take_requests().is_empty() {
                            Ok(values)
                        } else {
                            Err(DriverError::Conversion("a streamed row needs server text forms".to_string()))
                        }
                    })
                }),
                Some(Err(e)) => Some(Err(DriverError::from(e))),
                None => None,
            },
        }
    }
}

/// One `RowStreamIterator`'s state, shared with the `Transaction` it was opened on so that ending
/// the transaction can drop its portal even while Python still holds the iterator.
struct RowStreamSlot {
    /// `None` once the stream is exhausted, has raised, or was closed.
    state: Mutex<Option<OpenRowStream>>,
    /// Set by `close()`: a fetch in flight while it ran drops the stream once it finishes.
    close_requested: AtomicBool,
    /// Set when the owning transaction ended while the stream was still open.
    closed_by_transaction_end: AtomicBool,
}

impl RowStreamSlot {
    /// Drops the stream (and with it the portal) now, or - while a fetch holds it - as soon as
    /// that fetch finishes.
    fn close(&self, by_transaction_end: bool) {
        if by_transaction_end {
            self.closed_by_transaction_end.store(true, Ordering::Release);
        }
        self.close_requested.store(true, Ordering::Release);
        if let Ok(mut state) = self.state.try_lock() {
            *state = None;
        }
    }

    /// What a fetch on a stream that is no longer open raises.
    fn closed_error(&self) -> PyErr {
        if self.closed_by_transaction_end.load(Ordering::Acquire) {
            tx_finished()
        } else {
            stream_exhausted()
        }
    }
}

fn stream_exhausted() -> PyErr {
    pyo3::exceptions::PyStopAsyncIteration::new_err(())
}

/// `QuerySet.stream()`'s `rust_pg` row source - implements Python's async-iterator protocol
/// directly. Once the stream is exhausted, has raised or was closed, further `__anext__` calls
/// raise `StopAsyncIteration` (`TransactionFinishedError` when the transaction ended first)
/// rather than re-polling a finished stream.
#[pyclass]
pub struct RowStreamIterator {
    slot: Arc<RowStreamSlot>,
}

#[pymethods]
impl RowStreamIterator {
    fn __aiter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    /// Drops the stream and its portal right away, without reading the remaining rows - the
    /// connection's next statement no longer waits behind them. Idempotent.
    fn close(&self) {
        self.slot.close(false);
    }

    fn __anext__<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let slot = self.slot.clone();
        future_into_py(py, async move {
            let mut guard = slot.state.lock().await;
            if slot.close_requested.load(Ordering::Acquire) {
                *guard = None;
            }
            let Some(state) = guard.as_mut() else {
                return Err(slot.closed_error());
            };
            let next_values = state.next_values().await;
            let names = state.names.clone();
            let connection_poisoned = state.connection_poisoned.clone();
            // The stream is unusable after an error or its end - drop it so a later __anext__()
            // gets a clean StopAsyncIteration instead of polling a finished stream again. A
            // close() that arrived while this fetch held the stream drops it now as well.
            if !matches!(next_values, Some(Ok(_))) || slot.close_requested.load(Ordering::Acquire) {
                *guard = None;
            }
            drop(guard);
            match next_values {
                Some(Ok(values)) => Python::attach(|py| -> PyResult<Py<PyAny>> {
                    let py_values: Vec<Py<PyAny>> = values
                        .into_iter()
                        .map(|v| v.into_pyobject(py).map(pyo3::Bound::unbind))
                        .collect::<PyResult<_>>()?;
                    Ok(Py::new(py, PgRow::new(names, py_values))?.into_any())
                }),
                Some(Err(error)) => Transaction::note_outcome(&connection_poisoned, Err(error)),
                None => Err(stream_exhausted()),
            }
        })
    }

    /// Up to `size` rows - fewer only at the end of the stream, none once it is exhausted. An error
    /// met after some rows is raised by the next call, so those rows come first, as one by one.
    fn fetch_many<'py>(&self, py: Python<'py>, size: usize) -> PyResult<Bound<'py, PyAny>> {
        let slot = self.slot.clone();
        future_into_py(py, async move {
            let mut guard = slot.state.lock().await;
            if slot.close_requested.load(Ordering::Acquire) {
                *guard = None;
            }
            let Some(state) = guard.as_mut() else {
                if slot.closed_by_transaction_end.load(Ordering::Acquire) {
                    return Err(tx_finished());
                }
                return Python::attach(|py| -> PyResult<Py<PyAny>> { Ok(PyList::empty(py).into_any().unbind()) });
            };
            let mut batch = Vec::with_capacity(size.clamp(1, 1024));
            let mut failure = None;
            let mut ended = false;
            while batch.len() < size.max(1) {
                match state.next_values().await {
                    Some(Ok(values)) => batch.push(values),
                    Some(Err(error)) if batch.is_empty() => {
                        failure = Some(error);
                        break;
                    }
                    Some(Err(error)) => {
                        state.pending_error = Some(error);
                        break;
                    }
                    None => {
                        ended = true;
                        break;
                    }
                }
            }
            let names = state.names.clone();
            let connection_poisoned = state.connection_poisoned.clone();
            if failure.is_some() || ended || slot.close_requested.load(Ordering::Acquire) {
                *guard = None;
            }
            drop(guard);
            if let Some(error) = failure {
                return Transaction::note_outcome(&connection_poisoned, Err(error));
            }
            Python::attach(|py| -> PyResult<Py<PyAny>> {
                let rows = PyList::empty(py);
                for values in batch {
                    let py_values: Vec<Py<PyAny>> = values
                        .into_iter()
                        .map(|v| v.into_pyobject(py).map(pyo3::Bound::unbind))
                        .collect::<PyResult<_>>()?;
                    rows.append(Py::new(py, PgRow::new(names.clone(), py_values))?)?;
                }
                Ok(rows.into_any().unbind())
            })
        })
    }
}

#[cfg(test)]
mod isolation_level_tests {
    use super::*;

    #[test]
    fn validate_isolation_level_accepts_every_real_level_case_insensitively() {
        for (input, canonical) in [
            ("READ UNCOMMITTED", "READ UNCOMMITTED"),
            ("read committed", "READ COMMITTED"),
            ("Repeatable Read", "REPEATABLE READ"),
            ("serializable", "SERIALIZABLE"),
        ] {
            assert_eq!(validate_isolation_level(input).unwrap(), canonical);
        }
    }

    #[test]
    fn validate_isolation_level_rejects_sql_injection_attempt() {
        // The exact payload a `Client.begin(isolation=...)` caller could otherwise splice
        // straight into `BEGIN ISOLATION LEVEL {level}` via batch_execute's multi-statement
        // simple query protocol.
        let result = validate_isolation_level("SERIALIZABLE; DROP TABLE users; --");
        assert!(matches!(result, Err(DriverError::Config(_))), "expected Err(Config), got {result:?}");
    }

    #[test]
    fn validate_isolation_level_rejects_garbage_and_near_miss_values() {
        for bad in ["", "DROP TABLE users", "SERIALIZABLE READ ONLY", "READ COMMITTED;"] {
            let result = validate_isolation_level(bad);
            assert!(matches!(result, Err(DriverError::Config(_))), "{bad:?} should be rejected, got {result:?}");
        }
    }
}
