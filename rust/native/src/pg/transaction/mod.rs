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
//! `batch_execute`. Naming
//! and nesting-depth bookkeeping (which wrapper owns the transaction vs. which is a nested
//! savepoint reusing it) is entirely the Python side's responsibility
//! (the `rust_pg` driver's own `RustPgTransactionClient`) - this type only executes the
//! three savepoint SQL forms against whatever name it's given.
//!
//! BEGIN completes before this transaction is returned. Every later operation therefore runs
//! in an established transaction, including preparation and declaration of cursors.

use crate::pg::client::{CancelTarget, StatementCaching};
use crate::pg::connection_executor::PgConnectionExecutor;
use crate::pg::described_rows::DescribedRows;
use crate::pg::error::to_pyerr;
use crate::pg::error::ConnectionClosedError;
use crate::pg::error::DriverError;
use crate::pg::error::TransactionFinishedError;
use crate::pg::parameter_value_rows::ParameterValueRows;
use crate::pg::parameter_values::ParameterValues;
use crate::pg::result::PgResult;
use crate::pg::result_columns::ResultColumns;
use crate::pg::timed_manager::Object;
use crate::pg::tls::PgTls;
use crate::pg::value::Value;
use pyo3::prelude::*;
use pyo3_async_runtimes::tokio::future_into_py;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex as StdMutex, Weak};

/// The number of the next stream's cursor - its name, unique in the process: a pooler keeping
/// prepared statements by their text (PgBouncer) would hand a FETCH of a cursor of another
/// transaction, and its columns, to a cursor of the same name.
static STREAM_NUMBERS: AtomicU64 = AtomicU64::new(0);
use std::time::Duration;
use tokio::sync::Mutex;

mod closing_transaction;
mod cursor_rows;
use closing_transaction::ClosingTransaction;
mod row_stream_iterator;

use cursor_rows::CursorRows;
use row_stream_iterator::OpenRowStream;
pub use row_stream_iterator::RowStreamIterator;
use row_stream_iterator::RowStreamSlot;

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
        && name.chars().all(|character| character.is_ascii_alphanumeric() || character == '_')
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
    executor: PgConnectionExecutor,
    /// Every `RowStreamIterator` opened on this transaction - ending the transaction closes them.
    open_streams: Arc<StdMutex<Vec<Weak<RowStreamSlot>>>>,
}

impl Transaction {
    pub(crate) async fn begin(
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
        let transaction = Transaction {
            executor: PgConnectionExecutor::new(client, statement_caching, tls, cancel_target),
            open_streams: Arc::new(StdMutex::new(Vec::new())),
        };
        {
            let mut lease = transaction.executor.acquire().await?.expect("new transaction connection");
            let outcome = lease.client().batch_execute(&begin_sql).await.map_err(DriverError::from);
            lease.finish(outcome)?;
        }
        Ok(transaction)
    }

    /// Closes every registered iterator, discarding buffered rows and marking future fetches
    /// as transaction-finished. Each FETCH already drains its response; the server cursor itself
    /// ends with the transaction.
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
    async fn roll_back_and_release(
        closing: ClosingTransaction,
        connection_poisoned: &AtomicBool,
    ) -> Result<(), DriverError> {
        let outcome = match tokio::time::timeout(ROLLBACK_TIMEOUT, closing.connection().batch_execute("ROLLBACK")).await
        {
            Ok(outcome) => outcome.map_err(DriverError::from),
            Err(_) => Err(DriverError::Connection(format!(
                "ROLLBACK got no answer within {}s - the connection was closed, which aborts the transaction",
                ROLLBACK_TIMEOUT.as_secs()
            ))),
        };
        if matches!(&outcome, Err(error) if error.is_connection_lost()) {
            connection_poisoned.store(true, Ordering::Release);
        }
        closing.release(connection_poisoned);
        outcome
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
        let inner = self.executor.connection.clone();
        let pinned = self.executor.state.clone();
        Transaction::close_open_streams(&self.open_streams);
        pyo3_async_runtimes::tokio::get_runtime().spawn(async move {
            if let Some(client) = inner.lock().await.take() {
                let closing = ClosingTransaction::new(client, pinned.pending_cancels.clone());
                pinned.pending_cancels.wait_until_delivered().await;
                if pinned.connection_poisoned.load(Ordering::Acquire) {
                    // Closing the connection aborts the transaction server-side.
                    closing.release(&pinned.connection_poisoned);
                    return;
                }
                // The abandoned-mid-transaction safety net - commit()/rollback() take the
                // connection themselves before this ever runs.
                let _ = Transaction::roll_back_and_release(closing, &pinned.connection_poisoned).await;
            }
        });
    }
}

#[pymethods]
impl Transaction {
    fn invalidate_schema_cache(&self) {
        self.executor.caching.invalidate();
    }

    fn execute<'py>(&self, py: Python<'py>, sql: String, parameters: ParameterValues) -> PyResult<Bound<'py, PyAny>> {
        let params = parameters.0;
        let executor = self.executor.clone();
        future_into_py(py, async move {
            let mut lease = executor.acquire().await.map_err(to_pyerr)?.ok_or_else(tx_finished)?;
            lease.execute(&sql, &params, &executor.caching).await.map_err(to_pyerr)
        })
    }

    fn fetch_all<'py>(&self, py: Python<'py>, sql: String, parameters: ParameterValues) -> PyResult<Bound<'py, PyAny>> {
        let params = parameters.0;
        let executor = self.executor.clone();
        future_into_py(py, async move {
            let mut lease = executor.acquire().await.map_err(to_pyerr)?.ok_or_else(tx_finished)?;
            lease
                .fetch(&sql, &params, &executor.caching)
                .await
                .map(|(columns, data)| PgResult::new(columns, data))
                .map_err(to_pyerr)
        })
    }

    fn fetch_all_described<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        parameters: ParameterValues,
    ) -> PyResult<Bound<'py, PyAny>> {
        let params = parameters.0;
        let executor = self.executor.clone();
        future_into_py(py, async move {
            let mut lease = executor.acquire().await.map_err(to_pyerr)?.ok_or_else(tx_finished)?;
            lease
                .fetch(&sql, &params, &executor.caching)
                .await
                .map(|(columns, data)| DescribedRows { columns, data })
                .map_err(to_pyerr)
        })
    }

    /// `QuerySet.stream()`'s `rust_pg` execution primitive - declares a cursor of `sql`/`params` on
    /// this transaction's own pinned connection and hands back a `RowStreamIterator` fetching its rows
    /// a batch at a time.
    ///
    /// Each fetch reads its batch to the end, so a statement issued on this same transaction between
    /// two fetches - from the loop's body, or from a task sharing the transaction - runs at once: the
    /// rows of a portal read as they came off the wire held the connection until the last one was read,
    /// and such a statement waited behind them forever once they outgrew the connection's buffer.
    ///
    /// The stream is registered with this transaction: ending the transaction (commit, rollback,
    /// drop) closes it first, and its later `__anext__()` calls raise `TransactionFinishedError`. Its
    /// cursor ends with the transaction.
    fn stream<'py>(&self, py: Python<'py>, sql: String, parameters: ParameterValues) -> PyResult<Bound<'py, PyAny>> {
        let params = parameters.0;
        let executor = self.executor.clone();
        let caching = self.executor.caching.clone();
        let open_streams = self.open_streams.clone();
        let cursor_name = format!("hare_stream_{}", STREAM_NUMBERS.fetch_add(1, Ordering::Relaxed));
        future_into_py(py, async move {
            let mut lease = executor.acquire().await.map_err(to_pyerr)?.ok_or_else(tx_finished)?;
            let client = lease.client();
            let outcome: Result<_, DriverError> = async {
                let statement = caching.prepare_values(client, &sql, &params).await?;
                let columns = ResultColumns::of_statement(&statement);
                let bound = ParameterValues::as_sql_refs(&params);
                let declare_sql = format!("DECLARE \"{cursor_name}\" NO SCROLL CURSOR FOR {sql}");
                let declare = client.prepare_typed(&declare_sql, statement.params()).await?;
                client.execute(&declare, &bound).await?;
                Ok(columns)
            }
            .await;
            let columns = lease.finish(outcome).map_err(to_pyerr)?;
            let rows = CursorRows::new(cursor_name, executor.clone());
            let slot = Arc::new(RowStreamSlot {
                state: Mutex::new(Some(OpenRowStream { rows, columns, pending_error: None })),
                close_requested: AtomicBool::new(false),
                closed_by_transaction_end: AtomicBool::new(false),
            });
            // Registered while the connection lock is still held - commit()/rollback() close the
            // registered streams again once they hold that lock, so none can slip past them.
            Transaction::register_stream(&open_streams, &slot);
            drop(lease);
            Ok(RowStreamIterator { slot })
        })
    }

    /// Pipelined batch execute against the ALREADY-OPEN transaction - no nested BEGIN/COMMIT the
    /// way `Client::execute_many` wraps a fresh connection (the batch is already inside the
    /// caller's transaction). One `prepare`, then every row's `execute_raw()` fired without
    /// awaiting each individually, so `bulk_create/bulk_update` keep one round trip per batch
    /// inside `Transactions.atomic()` too.
    fn execute_many<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        parameter_rows: ParameterValueRows,
    ) -> PyResult<Bound<'py, PyAny>> {
        let rows = parameter_rows.0;
        let executor = self.executor.clone();
        let caching = self.executor.caching.clone();
        future_into_py(py, async move {
            if rows.is_empty() {
                return Ok(());
            }
            let types = ParameterValues::batch_types(&rows).map_err(to_pyerr)?;
            let mut lease = executor.acquire().await.map_err(to_pyerr)?.ok_or_else(tx_finished)?;
            let client = lease.client();
            let outcome: Result<(), DriverError> = async {
                let statement = caching.prepare(client, &sql, &types).await?;
                let futures = rows.iter().map(|row| client.execute_raw(&statement, row.iter()));
                futures_util::future::try_join_all(futures).await?;
                Ok(())
            }
            .await;
            lease.finish(outcome).map_err(to_pyerr)
        })
    }

    /// `bulk_create(use_copy=True)` inside the transaction - COPY BINARY on its own pinned
    /// connection, after its BEGIN, so the rows commit or roll back with it. See `crate::pg::copy`
    /// for why COPY BINARY needs `column_types` declared upfront. `schema` - the table's, None for
    /// the one the search path finds. `tags_comment` - the query tags' comment, empty without tags -
    /// ends the statement.
    #[pyo3(signature = (table, schema, columns, column_types, records, tags_comment = String::new()))]
    fn copy_in<'py>(
        &self,
        py: Python<'py>,
        table: String,
        schema: Option<String>,
        columns: Vec<String>,
        column_types: Vec<String>,
        records: Vec<Vec<Value>>,
        tags_comment: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let executor = self.executor.clone();
        future_into_py(py, async move {
            let mut lease = executor.acquire().await.map_err(to_pyerr)?.ok_or_else(tx_finished)?;
            let client = lease.client();
            let result = crate::pg::copy::copy_in_records(
                client,
                &table,
                schema.as_deref(),
                &columns,
                &column_types,
                records,
                &tags_comment,
            )
            .await;
            lease.finish(result).map_err(to_pyerr)
        })
    }

    /// Runs a (possibly multi-statement) script on this transaction's own pinned connection - a
    /// `TRUNCATE` from a second pool connection would block forever on the lock this transaction
    /// already holds.
    fn execute_script<'py>(&self, py: Python<'py>, sql: String) -> PyResult<Bound<'py, PyAny>> {
        self.run_on_connection(py, sql)
    }

    fn commit<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.executor.connection.clone();
        let pinned = self.executor.state.clone();
        let open_streams = self.open_streams.clone();
        // Stop existing iterators before waiting; repeat under the lock to catch a stream whose
        // declaration was still in flight when this transaction began closing.
        Transaction::close_open_streams(&open_streams);
        future_into_py(py, async move {
            let closing = ClosingTransaction::new(
                inner.lock().await.take().ok_or_else(tx_finished)?,
                pinned.pending_cancels.clone(),
            );
            let client = closing.connection();
            Transaction::close_open_streams(&open_streams);
            pinned.pending_cancels.wait_until_delivered().await;
            if client.is_closed() {
                // Certainly never reached the server - unlike an error while the COMMIT is in
                // flight, which leaves its outcome unknown.
                pinned.connection_poisoned.store(true, Ordering::Release);
                closing.release(&pinned.connection_poisoned);
                return Err(connection_closed_before("COMMIT"));
            }
            let outcome = client.batch_execute("COMMIT").await.map_err(DriverError::from);
            let result = Transaction::note_outcome(&pinned.connection_poisoned, outcome);
            closing.release(&pinned.connection_poisoned);
            result
        })
    }

    /// Releases the pinned connection back to the pool WITHOUT sending any SQL - for after
    /// `PREPARE TRANSACTION`, which already ended the SQL-level transaction block itself (a COMMIT
    /// or ROLLBACK would error with "no transaction in progress").
    fn finish_prepared<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.executor.connection.clone();
        let pinned = self.executor.state.clone();
        let open_streams = self.open_streams.clone();
        Transaction::close_open_streams(&open_streams);
        future_into_py(py, async move {
            let closing = ClosingTransaction::new(
                inner.lock().await.take().ok_or_else(tx_finished)?,
                pinned.pending_cancels.clone(),
            );
            Transaction::close_open_streams(&open_streams);
            pinned.pending_cancels.wait_until_delivered().await;
            closing.release(&pinned.connection_poisoned);
            Ok(())
        })
    }

    /// Bounded by `ROLLBACK_TIMEOUT`: past it the connection is closed (aborting the transaction
    /// server-side) and `ConnectionError` is raised.
    fn rollback<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.executor.connection.clone();
        let pinned = self.executor.state.clone();
        let open_streams = self.open_streams.clone();
        // See commit() for why this also runs before the lock is taken.
        Transaction::close_open_streams(&open_streams);
        future_into_py(py, async move {
            let closing = ClosingTransaction::new(
                inner.lock().await.take().ok_or_else(tx_finished)?,
                pinned.pending_cancels.clone(),
            );
            let client = closing.connection();
            Transaction::close_open_streams(&open_streams);
            pinned.pending_cancels.wait_until_delivered().await;
            if pinned.connection_poisoned.load(Ordering::Acquire) {
                // Closing the connection aborts the transaction server-side - no ROLLBACK for a
                // CancelRequest whose delivery was never confirmed to hit.
                closing.release(&pinned.connection_poisoned);
                return Ok(());
            }
            if client.is_closed() {
                pinned.connection_poisoned.store(true, Ordering::Release);
                closing.release(&pinned.connection_poisoned);
                return Err(connection_closed_before("ROLLBACK"));
            }
            Transaction::roll_back_and_release(closing, &pinned.connection_poisoned).await.map_err(to_pyerr)
        })
    }

    fn savepoint<'py>(&self, py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
        validate_identifier(name)?;
        self.run_on_connection(py, format!("SAVEPOINT {name}"))
    }

    fn release<'py>(&self, py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
        validate_identifier(name)?;
        self.run_on_connection(py, format!("RELEASE SAVEPOINT {name}"))
    }

    fn rollback_to<'py>(&self, py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
        validate_identifier(name)?;
        self.run_on_connection(py, format!("ROLLBACK TO SAVEPOINT {name}"))
    }
}

impl Transaction {
    /// Runs one simple-protocol statement (a savepoint statement, a script) on the pinned
    /// connection. BEGIN has already completed.
    fn run_on_connection<'py>(&self, py: Python<'py>, sql: String) -> PyResult<Bound<'py, PyAny>> {
        let executor = self.executor.clone();
        future_into_py(py, async move {
            let mut lease = executor.acquire().await.map_err(to_pyerr)?.ok_or_else(tx_finished)?;
            let client = lease.client();
            lease.finish(client.batch_execute(&sql).await.map_err(DriverError::from)).map_err(to_pyerr)
        })
    }
}

fn stream_exhausted() -> PyErr {
    pyo3::exceptions::PyStopAsyncIteration::new_err(())
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
