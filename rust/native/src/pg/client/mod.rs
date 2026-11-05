//! `Client` - a connection pool on tokio-postgres + deadpool-postgres + tokio-postgres-rustls. It
//! takes the connection fields the Python client parsed from the URL.
//!
//! Adapted from yara-orm (<https://github.com/vsdudakov/yara-orm>, MIT License): the TLS connection
//! setup - `CertCheck`, `SslParams`, the process-wide connector caches, `make_tls_connector()`,
//! `root_store()`, `build_tls_connector()` and the `AcceptAnyCert`/`ChainOnly` certificate verifiers,
//! with libpq's `sslmode` semantics - and `unified_param_types()`/`widen_pg_type()`. The rest is
//! hare-orm's own.

use crate::pg::closing_connect::ClosingConnect;
use crate::pg::connection_opening::open_connection;
use crate::pg::described_rows::DescribedRows;
use crate::pg::error::{to_pyerr, DriverError};
use crate::pg::parameter_value_rows::ParameterValueRows;
use crate::pg::parameter_values::ParameterValues;
use crate::pg::result::PgResult;
use crate::pg::timed_manager::{Pool, TimedManager};
use crate::pg::tls::{CertCheck, PgTls, SslParams};
use crate::pg::transaction::Transaction;
use crate::pg::value::Value;
use crate::pool::pool_checkout::PoolCheckout;
use crate::pool::pool_counters::PoolCounters;
use crate::pool::pool_metrics_switch::POOL_METRICS_ENABLED;
use crate::pool::pool_statistics::PoolStatistics;
use deadpool::managed::WeakPool;
use deadpool_postgres::{Manager, ManagerConfig, RecyclingMethod, Runtime, Timeouts};
use futures_util::future::{select, Either};
use futures_util::stream::poll_fn;
use futures_util::StreamExt;
use pyo3::prelude::*;
use pyo3::types::{PyWeakrefMethods, PyWeakrefReference};
use pyo3_async_runtimes::tokio::future_into_py;
use std::collections::HashMap;
use std::pin::pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex as StdMutex};
use std::task::{Context, Poll};
use std::time::{Duration, Instant};
use tokio::sync::watch;
use tokio_postgres::config::SslMode;
use tokio_postgres::tls::{MakeTlsConnect, TlsConnect};
use tokio_postgres::{AsyncMessage, NoTls, Socket};
use tokio_util::task::TaskTracker;

mod cancel_query_on_drop;
mod cancel_target;
mod listener;
mod pending_cancels;
mod pooled_connection;
mod statement_caching;

pub(crate) use cancel_query_on_drop::CancelQueryOnDrop;
pub(crate) use cancel_target::CancelTarget;
pub use listener::Listener;
pub(crate) use pending_cancels::PendingCancels;
pub use pooled_connection::PooledConnection;
pub(crate) use statement_caching::StatementCaching;

/// How long delivering one `CancelRequest` may take before the statements waiting for it (see
/// `PendingCancels`) go ahead anyway - the connection is then never returned to the pool.
const CANCEL_REQUEST_DELIVERY_TIMEOUT: Duration = Duration::from_secs(10);

/// How often a closing pool looks whether its checked-out connections came back.
const POOL_CLOSE_POLL_INTERVAL: Duration = Duration::from_millis(20);

/// How long a pooled connection may stay unused before it is closed, when the pool names no
/// `max_inactive_connection_lifetime` - asyncpg's own default.
const DEFAULT_MAX_INACTIVE_CONNECTION_LIFETIME: Duration = Duration::from_secs(300);

/// The shortest pause between two looks for unused connections to close.
const IDLE_CONNECTION_CHECK_MIN_INTERVAL: Duration = Duration::from_secs(1);

/// The port a config naming none connects to.
pub(crate) const POSTGRES_DEFAULT_PORT: u16 = 5432;

/// What every query guard on one transaction's pinned connection shares with the transaction.
#[derive(Clone)]
pub(crate) struct PinnedConnectionState {
    pub(crate) tls: PgTls,
    pub(crate) cancel_target: CancelTarget,
    /// Set when the connection must never go back to the pool (see `CancelQueryOnDrop`).
    pub(crate) connection_poisoned: Arc<AtomicBool>,
    pub(crate) pending_cancels: PendingCancels,
}

#[pyclass]
pub struct Client {
    pub(crate) pool: Pool,
    /// What the pool has done - the Python client's `PoolStatistics`, kept across its pools.
    pub(crate) counters: Arc<PoolCounters>,
    pub(crate) statement_caching: StatementCaching,
    pg_config: tokio_postgres::Config,
    tls: PgTls,
    cancel_target: CancelTarget,
    /// Set once the pool is closed for good - the connections still checked out end too.
    closed: Arc<watch::Sender<bool>>,
    connection_tasks: TaskTracker,
}

/// Escapes one `-c key=value` term of libpq's `options` string, which is split on unescaped
/// whitespace: a backslash escapes a space or a backslash, so a value with a space can't smuggle in
/// another `-c` option.
fn escape_options_term(term: &str) -> String {
    let mut escaped = String::with_capacity(term.len());
    for character in term.chars() {
        if character == '\\' || character == ' ' {
            escaped.push('\\');
        }
        escaped.push(character);
    }
    escaped
}

/// Closes the pooled connections left unused for `lifetime`, as asyncpg's
/// `max_inactive_connection_lifetime` does - looked for every half `lifetime` until the pool is
/// closed or dropped.
fn close_unused_connections(pool: WeakPool<TimedManager>, lifetime: Duration, mut closed: watch::Receiver<bool>) {
    let interval = (lifetime / 2).max(IDLE_CONNECTION_CHECK_MIN_INTERVAL);
    tokio::spawn(async move {
        loop {
            let pause = pin!(tokio::time::sleep(interval));
            let closing = pin!(closed.wait_for(|is_closed| *is_closed));
            if let Either::Right(_) = select(pause, closing).await {
                return;
            }
            let Some(pool) = pool.upgrade() else { return };
            if pool.is_closed() {
                return;
            }
            pool.retain(|_, metrics| metrics.last_used() < lifetime);
        }
    });
}

/// The deadpool-postgres manager opening the connections of `pg_config` - each ends once `closed`
/// is set.
fn build_manager(
    pg_config: &tokio_postgres::Config,
    tls: &PgTls,
    closed: watch::Receiver<bool>,
    connection_tasks: TaskTracker,
) -> Manager {
    let mgr_config = ManagerConfig { recycling_method: RecyclingMethod::Fast };
    match tls {
        PgTls::Disabled => {
            Manager::from_connect(pg_config.clone(), ClosingConnect::new(NoTls, closed, connection_tasks), mgr_config)
        }
        PgTls::Rustls(connector) => Manager::from_connect(
            pg_config.clone(),
            ClosingConnect::new(connector.clone(), closed, connection_tasks),
            mgr_config,
        ),
    }
}

#[pyfunction]
#[pyo3(signature = (
    host, port, user, password, database,
    min_size=1, max_size=16, statement_cache_size=None, application_name=None,
    ssl_mode=None, ssl_root_cert=None, server_settings=None, pool_acquire_timeout=None, statistics=None,
    max_inactive_connection_lifetime=None,
))]
#[allow(clippy::too_many_arguments)]
pub fn connect(
    py: Python<'_>,
    host: String,
    port: u16,
    user: String,
    password: Option<String>,
    database: Option<String>,
    min_size: usize,
    max_size: usize,
    statement_cache_size: Option<usize>,
    application_name: Option<String>,
    ssl_mode: Option<String>,
    ssl_root_cert: Option<String>,
    server_settings: Option<HashMap<String, String>>,
    // How long a caller waits for a free pool connection; None waits indefinitely, as asyncpg's
    // `Pool.acquire()` does by default.
    pool_acquire_timeout: Option<f64>,
    // What the pool does is counted in - the client's own, kept across its pools; a pool of its own
    // without.
    statistics: Option<Py<PoolStatistics>>,
    // Seconds a pooled connection may stay unused before it is closed, as asyncpg's option of the
    // same name; None takes asyncpg's default, 0 keeps unused connections open.
    max_inactive_connection_lifetime: Option<f64>,
) -> PyResult<Bound<'_, PyAny>> {
    let counters =
        statistics.map_or_else(|| Arc::new(PoolCounters::new(1)), |statistics| statistics.get().counters.clone());
    future_into_py(py, async move {
        let mut pg_config = tokio_postgres::Config::new();
        pg_config.host(&host).port(port).user(&user);
        // `database: None` (CREATE/DROP DATABASE run on another database) leaves the database
        // name to libpq's default - the user's own name, as asyncpg does.
        if let Some(db) = &database {
            pg_config.dbname(db);
        }
        if let Some(pw) = &password {
            pg_config.password(pw);
        }
        if let Some(app_name) = &application_name {
            pg_config.application_name(app_name);
        }
        // `Config::options()` replaces its value, so all `-c` options go in one string. Every
        // connection is pinned to UTC in the startup packet (no extra round trip); that option goes
        // last, so a caller's `server_settings` can't override it - the last `-c` of a setting wins.
        let mut options_parts: Vec<String> = Vec::new();
        if let Some(settings) = &server_settings {
            for (key, value) in settings {
                options_parts.push(format!("-c {}={}", escape_options_term(key), escape_options_term(value)));
            }
        }
        options_parts.push("-c TimeZone=UTC".to_string());
        pg_config.options(options_parts.join(" "));

        // libpq sslmode semantics: `disable` - no TLS; `prefer` (the default) and `require` encrypt
        // without authenticating the server; `verify-ca`/`verify-full` also check the certificate.
        // tokio-postgres knows disable/prefer/require; the verifying modes go to it as `require`,
        // with the check kept in `SslParams`.
        let mode = ssl_mode.as_deref().unwrap_or("prefer");
        // An unknown mode is an error: falling back to `prefer` would silently drop both the
        // verification and the encryption the caller asked for.
        let (driver_mode, ssl_params) = match mode {
            "disable" => (SslMode::Disable, SslParams::default()),
            "prefer" => (SslMode::Prefer, SslParams::default()),
            "require" => (SslMode::Require, SslParams::default()),
            "verify-ca" => (SslMode::Require, SslParams { check: CertCheck::Chain, root_cert: ssl_root_cert.clone() }),
            "verify-full" => (SslMode::Require, SslParams { check: CertCheck::Full, root_cert: ssl_root_cert.clone() }),
            other => {
                return Err(to_pyerr(DriverError::Config(format!(
                    "invalid ssl_mode {other:?}: expected one of \
                     disable/prefer/require/verify-ca/verify-full"
                ))));
            }
        };
        pg_config.ssl_mode(driver_mode);

        let tls = if driver_mode == SslMode::Disable {
            PgTls::Disabled
        } else {
            PgTls::Rustls(ssl_params.connector().map_err(to_pyerr)?)
        };
        // Cloned, not moved: the pool's manager (build_manager()) takes its own copy of both, and the
        // Client keeps its own - listen() reconnects later with the exact same settings.
        let (closed, closed_receiver) = watch::channel(false);
        let connection_tasks = TaskTracker::new();
        let mgr = build_manager(&pg_config, &tls, closed_receiver, connection_tasks.clone());

        let max_size = max_size.max(1);
        // Fallible conversion rejects negative/NaN durations without panicking across the Python boundary.
        let acquire_wait = match pool_acquire_timeout {
            None => None,
            Some(seconds) => Some(Duration::try_from_secs_f64(seconds).map_err(|_| {
                to_pyerr(DriverError::Config(format!(
                    "pool_acquire_timeout must be a non-negative number of seconds, got {seconds}"
                )))
            })?),
        };
        let inactive_lifetime = match max_inactive_connection_lifetime {
            None => Some(DEFAULT_MAX_INACTIVE_CONNECTION_LIFETIME),
            Some(0.0) => None,
            Some(seconds) => Some(Duration::try_from_secs_f64(seconds).map_err(|_| {
                to_pyerr(DriverError::Config(format!(
                    "max_inactive_connection_lifetime must be a non-negative number of seconds, got {seconds}"
                )))
            })?),
        };
        let pool = Pool::builder(TimedManager::new(mgr, counters.clone(), statement_cache_size))
            .max_size(max_size)
            // LIFO, as asyncpg's pool: prepared statements are cached per connection, and FIFO
            // would rotate a caller across every idle connection, so a statement run a few times
            // would never meet its warm cache.
            .queue_mode(deadpool_postgres::QueueMode::Lifo)
            .timeouts(Timeouts { wait: acquire_wait, create: None, recycle: None })
            // deadpool's own build() rejects a non-empty Timeouts without an explicit runtime
            // (it needs a timer facility to enforce `wait`) - this crate is tokio end to end
            // already (tokio_postgres, pyo3_async_runtimes::tokio), so Tokio1 is always correct
            // here, not just when pool_acquire_timeout is actually set.
            .runtime(Runtime::Tokio1)
            .build()
            .map_err(|error| to_pyerr(DriverError::Config(error.to_string())))?;

        // `pool.get()` on a freshly-created connection already round-trips through the full
        // Postgres startup/auth handshake - that's a real proof the connection works, so a
        // separate post-warm-up validation query would only add a redundant round trip without
        // catching any additional failure mode.
        let warm = min_size.max(1).min(max_size);
        let mut held = Vec::with_capacity(warm);
        for _ in 0..warm {
            match pool.get().await {
                Ok(pooled_connection) => held.push(pooled_connection),
                Err(error) => return Err(to_pyerr(DriverError::from_pool_error(error))),
            }
        }
        drop(held);
        if let Some(lifetime) = inactive_lifetime {
            close_unused_connections(pool.weak(), lifetime, closed.subscribe());
        }

        let cancel_target = CancelTarget::from_config(&pg_config);
        Ok(Client {
            pool,
            counters,
            statement_caching: StatementCaching::from_configured_size(statement_cache_size),
            pg_config,
            tls,
            cancel_target,
            closed: Arc::new(closed),
            connection_tasks,
        })
    })
}

#[pymethods]
impl Client {
    /// Invalidate statement and physical-type metadata without executing a command.
    fn invalidate_schema_cache(&self) {
        self.statement_caching.invalidate();
    }

    /// Opens the pool's connections from now on with `password` - a rotated credential. The open
    /// connections stay; a `listen()` reconnects with it too.
    fn set_password(&mut self, password: &str) {
        self.pg_config.password(password);
        self.pool.manager().replace(build_manager(
            &self.pg_config,
            &self.tls,
            self.closed.subscribe(),
            self.connection_tasks.clone(),
        ));
    }

    fn execute<'py>(&self, py: Python<'py>, sql: String, parameters: ParameterValues) -> PyResult<Bound<'py, PyAny>> {
        let params = parameters.0;
        let pool = self.pool.clone();
        let counters = self.counters.clone();
        let caching = self.statement_caching.clone();
        let tls = self.tls.clone();
        let cancel_target = self.cancel_target.clone();
        future_into_py(py, async move {
            let connection = PoolCheckout::get(&pool, &counters).await.map_err(to_pyerr)?;
            let pooled = PooledConnection::new(connection, caching, tls, cancel_target);
            let executor = &pooled.executor;
            let mut lease = executor.acquire().await.map_err(to_pyerr)?.ok_or_else(connection_released)?;
            let result = lease.execute(&sql, &params, &executor.caching).await.map_err(to_pyerr);
            drop(lease);
            executor.release().await;
            result
        })
    }

    fn fetch_all<'py>(&self, py: Python<'py>, sql: String, parameters: ParameterValues) -> PyResult<Bound<'py, PyAny>> {
        let params = parameters.0;
        let pool = self.pool.clone();
        let counters = self.counters.clone();
        let caching = self.statement_caching.clone();
        let tls = self.tls.clone();
        let cancel_target = self.cancel_target.clone();
        future_into_py(py, async move {
            let connection = PoolCheckout::get(&pool, &counters).await.map_err(to_pyerr)?;
            let pooled = PooledConnection::new(connection, caching, tls, cancel_target);
            let executor = &pooled.executor;
            let mut lease = executor.acquire().await.map_err(to_pyerr)?.ok_or_else(connection_released)?;
            let result = lease
                .fetch(&sql, &params, &executor.caching)
                .await
                .map(|(columns, data)| PgResult::new(columns, data))
                .map_err(to_pyerr);
            drop(lease);
            executor.release().await;
            result
        })
    }

    fn fetch_all_described<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        parameters: ParameterValues,
    ) -> PyResult<Bound<'py, PyAny>> {
        let params = parameters.0;
        let pool = self.pool.clone();
        let counters = self.counters.clone();
        let caching = self.statement_caching.clone();
        let tls = self.tls.clone();
        let cancel_target = self.cancel_target.clone();
        future_into_py(py, async move {
            let connection = PoolCheckout::get(&pool, &counters).await.map_err(to_pyerr)?;
            let pooled = PooledConnection::new(connection, caching, tls, cancel_target);
            let executor = &pooled.executor;
            let mut lease = executor.acquire().await.map_err(to_pyerr)?.ok_or_else(connection_released)?;
            let result = lease
                .fetch(&sql, &params, &executor.caching)
                .await
                .map(|(columns, data)| DescribedRows { columns, data })
                .map_err(to_pyerr);
            drop(lease);
            executor.release().await;
            result
        })
    }

    fn execute_many<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        parameter_rows: ParameterValueRows,
    ) -> PyResult<Bound<'py, PyAny>> {
        let rows = parameter_rows.0;
        let pool = self.pool.clone();
        let counters = self.counters.clone();
        let caching = self.statement_caching.clone();
        let tls = self.tls.clone();
        let cancel_target = self.cancel_target.clone();
        future_into_py(py, async move {
            if rows.is_empty() {
                return Ok(());
            }
            let types = ParameterValues::batch_types(&rows).map_err(to_pyerr)?;
            let connection = PoolCheckout::get(&pool, &counters).await.map_err(to_pyerr)?;
            let pooled = PooledConnection::new(connection, caching.clone(), tls, cancel_target);
            let executor = &pooled.executor;
            let mut lease = executor.acquire().await.map_err(to_pyerr)?.ok_or_else(connection_released)?;
            let outcome: Result<(), DriverError> = async {
                let client = lease.client_mut();
                let statement = caching.prepare(client, &sql, &types).await?;
                // execute_many's own transaction wraps just this batch, like asyncpg's
                // executemany() - not the caller's ambient transaction (if any).
                let transaction = client.transaction().await?;
                // Pipelined (one round trip for the batch, not one per row), through execute_raw:
                // no per-row Vec<&dyn ToSql> allocation and no parsing of RETURNING rows nobody reads.
                let futures = rows.iter().map(|row| transaction.execute_raw(&statement, row.iter()));
                futures_util::future::try_join_all(futures).await?;
                transaction.commit().await?;
                Ok(())
            }
            .await;
            let result = lease.finish(outcome).map_err(to_pyerr);
            drop(lease);
            executor.release().await;
            result
        })
    }

    /// `bulk_create(use_copy=True)`'s execution primitive - a fresh, non-transactional pool
    /// connection. See `crate::pg::copy` for why COPY BINARY needs `column_types` declared upfront.
    /// `schema` - the table's, None for the one the search path finds. `tags_comment` - the query
    /// tags' comment, empty without tags - ends the statement.
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
        let pool = self.pool.clone();
        let counters = self.counters.clone();
        let caching = self.statement_caching.clone();
        let tls = self.tls.clone();
        let cancel_target = self.cancel_target.clone();
        future_into_py(py, async move {
            let connection = PoolCheckout::get(&pool, &counters).await.map_err(to_pyerr)?;
            let pooled = PooledConnection::new(connection, caching.clone(), tls, cancel_target);
            let executor = &pooled.executor;
            let mut lease = executor.acquire().await.map_err(to_pyerr)?.ok_or_else(connection_released)?;
            let outcome = crate::pg::copy::copy_in_records(
                lease.client(),
                &table,
                schema.as_deref(),
                &columns,
                &column_types,
                records,
                &tags_comment,
            )
            .await;
            let result = lease.finish(outcome).map_err(to_pyerr);
            drop(lease);
            executor.release().await;
            result
        })
    }

    /// Runs a (possibly multi-statement, `;`-separated) script in one round trip through the
    /// simple-query protocol.
    fn execute_script<'py>(&self, py: Python<'py>, sql: String) -> PyResult<Bound<'py, PyAny>> {
        let pool = self.pool.clone();
        let counters = self.counters.clone();
        let caching = self.statement_caching.clone();
        let tls = self.tls.clone();
        let cancel_target = self.cancel_target.clone();
        future_into_py(py, async move {
            let connection = PoolCheckout::get(&pool, &counters).await.map_err(to_pyerr)?;
            let pooled = PooledConnection::new(connection, caching.clone(), tls, cancel_target);
            let executor = &pooled.executor;
            let mut lease = executor.acquire().await.map_err(to_pyerr)?.ok_or_else(connection_released)?;
            let outcome = lease.client().batch_execute(&sql).await.map_err(DriverError::from);
            let result = lease.finish(outcome).map_err(to_pyerr);
            drop(lease);
            executor.release().await;
            result
        })
    }

    fn begin<'py>(&self, py: Python<'py>, isolation: Option<String>) -> PyResult<Bound<'py, PyAny>> {
        let pool = self.pool.clone();
        let counters = self.counters.clone();
        let caching = self.statement_caching.clone();
        let tls = self.tls.clone();
        let cancel_target = self.cancel_target.clone();
        future_into_py(py, async move {
            let client = PoolCheckout::get(&pool, &counters).await.map_err(to_pyerr)?;
            Transaction::begin(client, isolation.as_deref(), caching, tls, cancel_target).await.map_err(to_pyerr)
        })
    }

    /// Takes a checked-out connection and returns a transaction after the server confirms BEGIN.
    fn begin_on<'py>(
        &self,
        py: Python<'py>,
        pooled_connection: &Bound<'_, PooledConnection>,
        isolation: Option<String>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let pooled = pooled_connection.borrow();
        let inner = pooled.executor.connection.clone();
        let pending_cancels = pooled.executor.state.pending_cancels.clone();
        let connection_lost = pooled.executor.state.connection_poisoned.clone();
        let caching = self.statement_caching.clone();
        let tls = self.tls.clone();
        let cancel_target = self.cancel_target.clone();
        future_into_py(py, async move {
            let mut guard = inner.lock().await;
            pending_cancels.wait_until_delivered().await;
            if connection_lost.load(Ordering::Acquire) {
                return Err(to_pyerr(DriverError::Connection("connection is no longer reusable".to_string())));
            }
            let client = guard.take().ok_or_else(connection_released)?;
            drop(guard);
            Transaction::begin(client, isolation.as_deref(), caching, tls, cancel_target).await.map_err(to_pyerr)
        })
    }

    /// Closes the pool: the idle connections at once, the ones checked out as they come back -
    /// those still out after `wait_seconds` are dropped, their transactions rolled back by the
    /// server, as asyncpg's `Pool.terminate()` does.
    fn close<'py>(&self, py: Python<'py>, wait_seconds: f64) -> PyResult<Bound<'py, PyAny>> {
        let wait = Duration::try_from_secs_f64(wait_seconds).map_err(|_| {
            to_pyerr(DriverError::Config(format!("close() waits a non-negative number of seconds, got {wait_seconds}")))
        })?;
        let pool = self.pool.clone();
        let closed = self.closed.clone();
        let connection_tasks = self.connection_tasks.clone();
        future_into_py(py, async move {
            pool.close();
            let deadline = Instant::now() + wait;
            // A connection still being opened for a caller is in no size yet - it is one of the
            // pool's users, counted as waiting.
            while Instant::now() < deadline {
                let status = pool.status();
                if status.size == 0 && status.waiting == 0 {
                    break;
                }
                tokio::time::sleep(POOL_CLOSE_POLL_INTERVAL).await;
            }
            closed.send_replace(true);
            // Closing the watch alone only schedules driver shutdown. Wait for every active
            // driver future to drop its socket before a former transaction can attempt COMMIT.
            connection_tasks.close();
            connection_tasks.wait().await;
            Ok(())
        })
    }

    /// Closes every idle pooled connection, as asyncpg's `Pool.expire_connections()`; a connection
    /// checked out right now is not touched.
    fn expire_connections<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let pool = self.pool.clone();
        future_into_py(py, async move {
            pool.retain(|_, _| false);
            Ok(())
        })
    }

    /// Checks out one plain connection outside any transaction - what `acquire()`/`release()` of a
    /// pool connection wrapper do. Dropping it returns it to the pool; it was never in a transaction,
    /// so nothing needs rolling back first. `waited_seconds` - how long the caller waited for its turn
    /// before (a transaction's slot): counted with the check-out's own wait, as one.
    #[pyo3(signature = (waited_seconds = 0.0))]
    fn acquire<'py>(&self, py: Python<'py>, waited_seconds: f64) -> PyResult<Bound<'py, PyAny>> {
        let pool = self.pool.clone();
        let counters = self.counters.clone();
        let statement_caching = self.statement_caching.clone();
        let tls = self.tls.clone();
        let cancel_target = self.cancel_target.clone();
        let already_waited = Duration::try_from_secs_f64(waited_seconds).unwrap_or(Duration::ZERO);
        future_into_py(py, async move {
            let client = PoolCheckout::get_after(&pool, &counters, already_waited).await.map_err(to_pyerr)?;
            Ok(PooledConnection::new(client, statement_caching, tls, cancel_target))
        })
    }

    /// Checks out a pooled connection right away, without a trip to the tokio runtime, when one is
    /// idle - `acquire()`'s check-out takes no I/O then (`RecyclingMethod::Fast` only looks at
    /// whether the connection is closed), so a single poll completes it. None when the pool would
    /// make the caller wait or open a new connection: the caller then awaits `acquire()`.
    #[pyo3(signature = (waited_seconds = 0.0))]
    fn try_acquire(&self, waited_seconds: f64) -> PyResult<Option<PooledConnection>> {
        if self.pool.status().available == 0 {
            return Ok(None);
        }
        // The pool's wait timeout is a tokio timer - polled inside the runtime's context.
        let _runtime_context = pyo3_async_runtimes::tokio::get_runtime().enter();
        let mut check_out = std::pin::pin!(self.pool.get());
        let mut context = Context::from_waker(std::task::Waker::noop());
        match std::future::Future::poll(check_out.as_mut(), &mut context) {
            Poll::Ready(Ok(client)) => {
                // Taken without waiting - the caller's earlier wait alone, when the waits are measured.
                PoolCheckout::count_taken(
                    &self.counters,
                    POOL_METRICS_ENABLED
                        .load(Ordering::Relaxed)
                        .then(|| Duration::try_from_secs_f64(waited_seconds).unwrap_or(Duration::ZERO)),
                );
                Ok(Some(PooledConnection::new(
                    client,
                    self.statement_caching.clone(),
                    self.tls.clone(),
                    self.cancel_target.clone(),
                )))
            }
            Poll::Ready(Err(error)) => Err(to_pyerr(PoolCheckout::get_error(&self.counters, error))),
            Poll::Pending => Ok(None),
        }
    }

    /// How the pool is taken: its open connections, the idle ones, the callers waiting for one and
    /// its most connections.
    fn get_pool_status(&self) -> (usize, usize, usize, usize) {
        let status = self.pool.status();
        (status.size, status.available, status.waiting, status.max_size)
    }

    /// Returns a connection from `acquire()` to the pool and waits until it is back, so a statement
    /// right after (a `DROP DATABASE`) never races it.
    #[allow(clippy::unused_self, reason = "a method of the Python pool object")]
    fn release<'py>(&self, pooled_connection: &Bound<'py, PooledConnection>) -> PyResult<Bound<'py, PyAny>> {
        let executor = pooled_connection.borrow().executor.clone();
        future_into_py(pooled_connection.py(), async move {
            executor.release().await;
            Ok(())
        })
    }

    /// Opens a dedicated connection outside the pool and LISTENs on `channel`, calling `callback` (a
    /// plain callable) as `callback(listener, pid, channel, payload)` for every notification - the
    /// shape asyncpg's `listen()` has. The caller closes the returned `Listener`. NOTIFY needs no
    /// method: it is plain SQL on any connection.
    fn listen<'py>(&self, py: Python<'py>, channel: String, callback: Py<PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let config = self.pg_config.clone();
        let tls = self.tls.clone();
        let closed = Arc::new(AtomicBool::new(false));
        let listener = Py::new(py, Listener { inner: Arc::new(StdMutex::new(None)), closed: closed.clone() })?;
        // A WEAK reference, not `listener.clone_ref(py)` - see `open_listener`'s own doc comment
        // for why a strong `Py<Listener>` here is the actual root cause of a permanent leak.
        let listener_weak = PyWeakrefReference::new(listener.bind(py))?.unbind();
        future_into_py(py, async move {
            let client = match tls {
                PgTls::Disabled => open_listener(config, NoTls, channel, callback, listener_weak, closed).await,
                PgTls::Rustls(connector) => {
                    open_listener(config, connector, channel, callback, listener_weak, closed).await
                }
            }
            .map_err(|error| to_pyerr(DriverError::from(error)))?;
            Python::attach(|py| {
                *listener.borrow(py).inner.lock().unwrap_or_else(std::sync::PoisonError::into_inner) = Some(client);
            });
            Ok(listener)
        })
    }
}

/// Opens a dedicated connection with the pool's own settings, spawns the task that drives it and
/// hands every notification to `callback`, then sends `LISTEN channel`. Generic over the TLS choice,
/// since the connection's type depends on it.
///
/// The task holds `listener` only weakly: a `Listener` the caller drops without `close()` is freed by
/// ordinary reference counting, which drops the only `tokio_postgres::Client` and closes the
/// connection, ending the task.
async fn open_listener<T>(
    config: tokio_postgres::Config,
    tls: T,
    channel: String,
    callback: Py<PyAny>,
    listener: Py<PyWeakrefReference>,
    closed: Arc<AtomicBool>,
) -> Result<tokio_postgres::Client, tokio_postgres::Error>
where
    T: MakeTlsConnect<Socket> + Clone + Send + 'static,
    T::TlsConnect: Send,
    T::Stream: Send,
    <T::TlsConnect as TlsConnect<Socket>>::Future: Send,
{
    let (client, mut connection) = open_connection(&config, tls).await?;
    tokio::spawn(async move {
        // `poll_message()`: only it surfaces the notifications, a plainly spawned connection
        // future drives I/O alone.
        let mut messages = poll_fn(move |cx| connection.poll_message(cx));
        while let Some(item) = messages.next().await {
            let notification = match item {
                Ok(AsyncMessage::Notification(notification)) => notification,
                // A notice (not a notification) - silently skipped (this crate's other query
                // paths never surface those either).
                Ok(_) => continue,
                // The notification stream broke (connection lost, backend terminated): `closed`
                // tells the caller the listener went silent.
                Err(_) => {
                    closed.store(true, Ordering::Release);
                    break;
                }
            };
            let pid = notification.process_id();
            let channel_name = notification.channel().to_string();
            let payload = notification.payload().to_string();
            Python::attach(|py| {
                // The `Listener` was collected - the connection is closing and the loop ends.
                let Some(listener) = listener.bind(py).upgrade() else {
                    return;
                };
                let callback = callback.clone_ref(py);
                // A callback that raises is reported, not silently swallowed - but must never be
                // allowed to kill this loop, or every notification after the first bad one would
                // silently stop being delivered.
                if let Err(err) = callback.call1(py, (listener, pid, channel_name, payload)) {
                    err.print(py);
                }
            });
        }
    });

    let listen_sql = format!("LISTEN \"{}\"", channel.replace('"', "\"\""));
    client.batch_execute(&listen_sql).await?;
    Ok(client)
}

fn connection_released() -> PyErr {
    pyo3::exceptions::PyRuntimeError::new_err("connection already released back to the pool")
}

#[cfg(test)]
mod cancel_delivery_tests {
    use super::cancel_target::CloseAwaitingStream;
    use super::*;
    use std::future::Future;
    use std::sync::atomic::AtomicUsize;
    use std::task::Poll;
    use tokio::net::TcpStream;
    use tokio::sync::oneshot;

    fn runtime() -> tokio::runtime::Runtime {
        tokio::runtime::Builder::new_multi_thread().enable_all().build().expect("a test runtime")
    }

    #[test]
    fn wait_until_delivered_waits_for_every_pending_delivery() {
        runtime().block_on(async {
            let pending_cancels = PendingCancels::default();
            let finished = Arc::new(AtomicUsize::new(0));
            let mut releases = Vec::new();
            for _ in 0..3 {
                let finished = finished.clone();
                let (release, gate) = oneshot::channel();
                let (completed, completion) = oneshot::channel();
                releases.push((release, completion));
                pending_cancels.add(tokio::spawn(async move {
                    gate.await.expect("delivery released");
                    finished.fetch_add(1, Ordering::SeqCst);
                    completed.send(()).expect("delivery observed");
                }));
            }
            let mut waiter = Box::pin(pending_cancels.wait_until_delivered());
            for (release, completion) in releases {
                std::future::poll_fn(|context| {
                    assert!(matches!(waiter.as_mut().poll(context), Poll::Pending));
                    Poll::Ready(())
                })
                .await;
                release.send(()).expect("delivery task waiting");
                completion.await.expect("delivery completed");
            }
            waiter.await;
            assert_eq!(finished.load(Ordering::SeqCst), 3);
            // Nothing pending any more - returns at once.
            pending_cancels.wait_until_delivered().await;
        });
    }

    #[test]
    fn a_single_tcp_host_gets_a_delivery_confirming_cancel_target() {
        let mut config = tokio_postgres::Config::new();
        config.host("db.example").port(6543);
        assert!(matches!(
            CancelTarget::from_config(&config),
            CancelTarget::Tcp { ref host, port: 6543 } if host == "db.example"
        ));
        config.host("second.example");
        assert!(matches!(CancelTarget::from_config(&config), CancelTarget::Other));
    }

    #[test]
    fn the_cancel_connection_shutdown_waits_until_the_server_closes_it() {
        runtime().block_on(async {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.expect("a local listener");
            let address = listener.local_addr().expect("a bound address");
            let server_closed = Arc::new(AtomicBool::new(false));
            let server_closed_by_server = server_closed.clone();
            let (request_read, request_received) = oneshot::channel();
            let (release, gate) = oneshot::channel();
            let server = tokio::spawn(async move {
                let (mut socket, _) = listener.accept().await.expect("an accepted connection");
                let mut request = Vec::new();
                tokio::io::AsyncReadExt::read_to_end(&mut socket, &mut request).await.expect("the request");
                request_read.send(()).expect("request observed");
                gate.await.expect("server close released");
                server_closed_by_server.store(true, Ordering::SeqCst);
                drop(socket);
                request
            });
            let socket = TcpStream::connect(address).await.expect("a client connection");
            let mut stream = CloseAwaitingStream { socket, write_side_closed: false };
            tokio::io::AsyncWriteExt::write_all(&mut stream, b"cancel").await.expect("the write");
            let mut shutdown = Box::pin(tokio::io::AsyncWriteExt::shutdown(&mut stream));
            match select(&mut shutdown, request_received).await {
                Either::Left((result, _)) => panic!("shutdown returned before server close: {result:?}"),
                Either::Right((received, _)) => received.expect("server read the request"),
            }
            std::future::poll_fn(|context| {
                assert!(matches!(shutdown.as_mut().poll(context), Poll::Pending));
                Poll::Ready(())
            })
            .await;
            assert!(!server_closed.load(Ordering::SeqCst));
            release.send(()).expect("server waiting to close");
            shutdown.await.expect("the shutdown");
            assert!(server_closed.load(Ordering::SeqCst), "shutdown returned before the server closed the connection");
            assert_eq!(server.await.expect("the server task"), b"cancel");
        });
    }
}
