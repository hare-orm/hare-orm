//! `Client` - a connection pool on tokio-postgres + deadpool-postgres + tokio-postgres-rustls. It
//! takes the connection fields the Python client parsed from the URL.
//!
//! Adapted from yara-orm (<https://github.com/vsdudakov/yara-orm>, MIT License): the TLS connection
//! setup - `CertCheck`, `SslParams`, the process-wide connector caches, `make_tls_connector()`,
//! `root_store()`, `build_tls_connector()` and the `AcceptAnyCert`/`ChainOnly` certificate verifiers,
//! with libpq's `sslmode` semantics - and `unified_param_types()`/`widen_pg_type()`. The rest is
//! hare-orm's own.

use crate::asyncio::completion::future_into_py;
use crate::pg::closing_connect::ClosingConnect;
use crate::pg::described_rows::DescribedRows;
use crate::pg::error::{to_pyerr, DriverError};
use crate::pg::first_row::FirstRow;
use crate::pg::one_round_trip::{binds_in_one_round_trip, get_result_columns, typed_parameters};
use crate::pg::parameter_value_rows::ParameterValueRows;
use crate::pg::parameter_values::ParameterValues;
use crate::pg::result::PgResult;
use crate::pg::result_columns::ResultColumns;
use crate::pg::result_data::ResultData;
use crate::pg::timed_manager::{Object, Pool, TimedManager};
use crate::pg::transaction::Transaction;
use crate::pg::value::{decode_pg_row_values, RawWireValue, ServerTextForms, Value, SERVER_TEXT_FORMS_PER_QUERY};
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
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::client::WebPkiServerVerifier;
use rustls::crypto::{verify_tls12_signature, verify_tls13_signature, CryptoProvider};
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::{CertificateError, DigitallySignedStruct, Error as RustlsError, RootCertStore, SignatureScheme};
use std::collections::HashMap;
use std::fs::File;
use std::io::BufReader;
use std::pin::pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex as StdMutex, OnceLock};
use std::task::{Context, Poll};
use std::time::{Duration, Instant};
use tokio::sync::watch;
use tokio_postgres::config::SslMode;
use tokio_postgres::tls::{MakeTlsConnect, TlsConnect};
use tokio_postgres::types::{Kind as TypeShape, ToSql, Type};
use tokio_postgres::{AsyncMessage, NoTls, Socket, Statement};
use tokio_postgres_rustls::MakeRustlsConnect;

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

/// How far the server's certificate is checked, per libpq's `sslmode` semantics (the semantics
/// callers coming from asyncpg/psycopg were written against): `prefer`/`require` encrypt without
/// authenticating; `verify-ca`/`verify-full` are the modes that actually check the certificate.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
enum CertCheck {
    #[default]
    None,
    Chain,
    Full,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
struct SslParams {
    check: CertCheck,
    root_cert: Option<String>,
}

static TLS_NO_VERIFY: OnceLock<MakeRustlsConnect> = OnceLock::new();

static TLS_VERIFY_CHAIN: OnceLock<MakeRustlsConnect> = OnceLock::new();

static TLS_VERIFY_FULL: OnceLock<MakeRustlsConnect> = OnceLock::new();

fn make_tls_connector(params: &SslParams) -> Result<MakeRustlsConnect, DriverError> {
    if params.root_cert.is_some() {
        return build_tls_connector(params);
    }
    let cache = match params.check {
        CertCheck::None => &TLS_NO_VERIFY,
        CertCheck::Chain => &TLS_VERIFY_CHAIN,
        CertCheck::Full => &TLS_VERIFY_FULL,
    };
    if let Some(connector) = cache.get() {
        return Ok(connector.clone());
    }
    let connector = build_tls_connector(params)?;
    Ok(cache.get_or_init(|| connector).clone())
}

fn root_store(extra_ca: Option<&str>) -> Result<RootCertStore, DriverError> {
    let mut roots = RootCertStore::empty();
    for cert in rustls_native_certs::load_native_certs().certs {
        let _ = roots.add(cert);
    }
    let Some(path) = extra_ca else {
        return Ok(roots);
    };
    let file = File::open(path)
        .map_err(|error| DriverError::Config(format!("ssl_root_cert {path:?} cannot be read: {error}")))?;
    let mut added = 0usize;
    for cert in rustls_pemfile::certs(&mut BufReader::new(file)) {
        let cert = cert.map_err(|error| DriverError::Config(format!("ssl_root_cert {path:?} is not PEM: {error}")))?;
        roots.add(cert).map_err(|error| DriverError::Config(format!("ssl_root_cert {path:?} is unusable: {error}")))?;
        added += 1;
    }
    if added == 0 {
        return Err(DriverError::Config(format!("ssl_root_cert {path:?} holds no certificates")));
    }
    Ok(roots)
}

fn build_tls_connector(params: &SslParams) -> Result<MakeRustlsConnect, DriverError> {
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let builder = rustls::ClientConfig::builder_with_provider(provider.clone())
        .with_safe_default_protocol_versions()
        .map_err(|error| DriverError::Config(error.to_string()))?;
    let verifier: Arc<dyn ServerCertVerifier> = match params.check {
        CertCheck::None => Arc::new(AcceptAnyCert(provider)),
        check => {
            let roots = root_store(params.root_cert.as_deref())?;
            let webpki = WebPkiServerVerifier::builder_with_provider(Arc::new(roots), provider)
                .build()
                .map_err(|error| DriverError::Config(error.to_string()))?;
            if check == CertCheck::Full {
                webpki
            } else {
                Arc::new(ChainOnly(webpki))
            }
        }
    };
    let config = builder.dangerous().with_custom_certificate_verifier(verifier).with_no_client_auth();
    Ok(MakeRustlsConnect::new(config))
}

/// Accepts any server certificate - the verifier behind libpq's `prefer`/`require`.
#[derive(Debug)]
struct AcceptAnyCert(Arc<CryptoProvider>);

impl ServerCertVerifier for AcceptAnyCert {
    fn verify_server_cert(
        &self,
        _end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, RustlsError> {
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, RustlsError> {
        verify_tls12_signature(message, cert, dss, &self.0.signature_verification_algorithms)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, RustlsError> {
        verify_tls13_signature(message, cert, dss, &self.0.signature_verification_algorithms)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.0.signature_verification_algorithms.supported_schemes()
    }
}

/// libpq's `verify-ca`: the chain is verified, the hostname is not.
#[derive(Debug)]
struct ChainOnly(Arc<WebPkiServerVerifier>);

impl ServerCertVerifier for ChainOnly {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        intermediates: &[CertificateDer<'_>],
        server_name: &ServerName<'_>,
        ocsp_response: &[u8],
        now: UnixTime,
    ) -> Result<ServerCertVerified, RustlsError> {
        match self.0.verify_server_cert(end_entity, intermediates, server_name, ocsp_response, now) {
            Err(RustlsError::InvalidCertificate(
                CertificateError::NotValidForName | CertificateError::NotValidForNameContext { .. },
            )) => Ok(ServerCertVerified::assertion()),
            other => other,
        }
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, RustlsError> {
        self.0.verify_tls12_signature(message, cert, dss)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, RustlsError> {
        self.0.verify_tls13_signature(message, cert, dss)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.0.supported_verify_schemes()
    }
}

/// The unspecified parameter type (OID 0) - the server infers the parameter's type from context.
/// Built once: `Type::new()` allocates, and most bind parameters need this type.
pub(crate) fn unspecified_type() -> Type {
    static UNSPECIFIED: OnceLock<Type> = OnceLock::new();
    UNSPECIFIED.get_or_init(|| Type::new(String::new(), 0, TypeShape::Simple, String::new())).clone()
}

pub(crate) async fn prepare_for(
    client: &Object,
    sql: &str,
    params: &[Value],
    caching: &StatementCaching,
) -> Result<Statement, DriverError> {
    let types: Vec<Type> = params.iter().map(|value| value.pg_type().unwrap_or_else(unspecified_type)).collect();
    Ok(caching.prepare(client, sql, &types).await?)
}

/// SQLSTATE 0A000 (`feature_not_supported`) covers more than this one case - narrow the match to
/// the exact server message text plancache.c raises this specific error with, so an unrelated
/// feature-not-supported failure never gets silently retried as if it were this one.
fn is_stale_cached_plan_error(error: &tokio_postgres::Error) -> bool {
    error.as_db_error().is_some_and(|db| {
        db.code().code() == "0A000" && db.message().contains("cached plan must not change result type")
    })
}

/// Prepares `sql` through the statement cache and runs `run` on it; when the cached plan is stale
/// (a DDL change since it was prepared), evicts it and runs once more on a fresh one. Not used inside
/// a transaction: the failed statement has already aborted it.
pub(crate) async fn prepare_and_run_with_stale_plan_retry<T, F, Fut>(
    client: &Object,
    sql: &str,
    params: &[Value],
    caching: &StatementCaching,
    run: F,
) -> Result<(Statement, T), DriverError>
where
    F: Fn(Statement) -> Fut,
    Fut: std::future::Future<Output = Result<T, tokio_postgres::Error>>,
{
    let types: Vec<Type> = params.iter().map(|value| value.pg_type().unwrap_or_else(unspecified_type)).collect();
    let statement = caching.prepare(client, sql, &types).await?;
    match run(statement.clone()).await {
        Ok(value) => Ok((statement, value)),
        Err(error) if caching.enabled && is_stale_cached_plan_error(&error) => {
            client.statement_cache.remove(sql, &types);
            let fresh_stmt = client.prepare_typed(sql, &types).await?;
            let value = run(fresh_stmt.clone()).await?;
            Ok((fresh_stmt, value))
        }
        Err(error) => Err(DriverError::from(error)),
    }
}

pub(crate) fn as_sql_params(params: &[Value]) -> Vec<&(dyn ToSql + Sync)> {
    params.iter().map(|value| value as &(dyn ToSql + Sync)).collect()
}

/// Decodes result rows, asking the server (on the same connection) for the text form of every
/// value of a type this driver has no binary decoder for - see `ServerTextForms`.
pub(crate) async fn decode_rows(
    client: &tokio_postgres::Client,
    rows: &[tokio_postgres::Row],
) -> Result<Vec<Vec<Value>>, DriverError> {
    let mut forms = ServerTextForms::collecting();
    let decoded = rows.iter().map(|row| decode_pg_row_values(row, &mut forms)).collect::<Result<Vec<_>, _>>()?;
    let requests = forms.take_requests();
    if requests.is_empty() {
        return Ok(decoded);
    }
    let texts = fetch_server_text_forms(client, &requests).await?;
    let mut forms = ServerTextForms::supplying(texts);
    rows.iter().map(|row| decode_pg_row_values(row, &mut forms)).collect()
}

/// Sends raw binary values back as parameters of their own type cast to text - the type's own
/// output function, exactly what a text-format result would have carried.
async fn fetch_server_text_forms(
    client: &tokio_postgres::Client,
    requests: &[(Type, &[u8])],
) -> Result<Vec<String>, DriverError> {
    let mut texts = Vec::with_capacity(requests.len());
    for chunk in requests.chunks(SERVER_TEXT_FORMS_PER_QUERY) {
        let placeholders = (1..=chunk.len()).map(|index| format!("${index}::text")).collect::<Vec<_>>().join(",");
        let sql = format!("SELECT ARRAY[{placeholders}]::text[]");
        let raw_values: Vec<RawWireValue> = chunk.iter().map(|(_, raw)| RawWireValue(raw)).collect();
        let params: Vec<(&(dyn ToSql + Sync), Type)> = raw_values
            .iter()
            .zip(chunk)
            .map(|(raw_value, (postgres_type, _))| (raw_value as &(dyn ToSql + Sync), postgres_type.clone()))
            .collect();
        let rows = client.query_typed(&sql, &params).await?;
        let row = rows.first().ok_or_else(|| DriverError::Conversion("text-form query returned no row".to_string()))?;
        match decode_pg_row_values(row, &mut ServerTextForms::collecting())?.into_iter().next() {
            Some(Value::Array(items)) => {
                for item in items {
                    match item {
                        Value::Text(text) => texts.push(text),
                        other => return Err(DriverError::Conversion(format!("unexpected text-form value {other:?}"))),
                    }
                }
            }
            other => return Err(DriverError::Conversion(format!("unexpected text-form result {other:?}"))),
        }
    }
    Ok(texts)
}

/// The TLS settings of the pool, kept for the dedicated connection `Client::listen()` opens.
#[derive(Clone)]
pub(crate) enum PgTls {
    Disabled,
    Rustls(MakeRustlsConnect),
}

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
fn build_manager(pg_config: &tokio_postgres::Config, tls: &PgTls, closed: watch::Receiver<bool>) -> Manager {
    let mgr_config = ManagerConfig { recycling_method: RecyclingMethod::Fast };
    match tls {
        PgTls::Disabled => Manager::from_connect(pg_config.clone(), ClosingConnect::new(NoTls, closed), mgr_config),
        PgTls::Rustls(connector) => {
            Manager::from_connect(pg_config.clone(), ClosingConnect::new(connector.clone(), closed), mgr_config)
        }
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
        // `Config::options()` REPLACES its stored string on every call, it does not append -
        // build one combined `-c k=v -c k=v ...` string and set it ONCE. Pin every connection to
        // UTC via a startup-packet option instead of a post-connect `SET TIME ZONE` query -
        // applies within the handshake tokio-postgres already pays for, no extra round trip per
        // connection. Ours goes LAST so it stays a hard hare correctness requirement (hare
        // stores/returns aware timestamps in UTC, so a non-UTC server TimeZone would make
        // server-side EXTRACT/CURRENT_TIMESTAMP depend on the server's locale) rather than
        // something a caller's own `server_settings` could override - Postgres applies repeated
        // `-c` options for the same GUC in order, last one winning.
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
            PgTls::Rustls(make_tls_connector(&ssl_params).map_err(to_pyerr)?)
        };
        // Cloned, not moved: the pool's manager (build_manager()) takes its own copy of both, and the
        // Client keeps its own - listen() reconnects later with the exact same settings.
        let (closed, closed_receiver) = watch::channel(false);
        let mgr = build_manager(&pg_config, &tls, closed_receiver);

        let max_size = max_size.max(1);
        // Validated on the Python side too - a negative/NaN value must never reach
        // Duration::from_secs_f64(), which panics on it.
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
        let pool = Pool::builder(TimedManager::new(mgr, counters.clone()))
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
        })
    })
}

#[pymethods]
impl Client {
    /// Opens the pool's connections from now on with `password` - a rotated credential. The open
    /// connections stay; a `listen()` reconnects with it too.
    fn set_password(&mut self, password: &str) {
        self.pg_config.password(password);
        self.pool.manager().replace(build_manager(&self.pg_config, &self.tls, self.closed.subscribe()));
    }

    fn execute<'py>(&self, py: Python<'py>, sql: String, parameters: ParameterValues) -> PyResult<Bound<'py, PyAny>> {
        let params = parameters.0;
        let pool = self.pool.clone();
        let counters = self.counters.clone();
        let caching = self.statement_caching.clone();
        let tls = self.tls.clone();
        let cancel_target = self.cancel_target.clone();
        future_into_py(py, async move {
            let pooled_connection = PoolCheckout::get(&pool, &counters).await.map_err(to_pyerr)?;
            let mut cancel_guard = CancelQueryOnDrop::for_pooled_connection(pooled_connection, tls, cancel_target);
            let client = cancel_guard.connection();
            let bound = as_sql_params(&params);
            let result = if binds_in_one_round_trip(&caching, &params) {
                client.execute_typed(&sql, &typed_parameters(&params)).await.map_err(DriverError::from)
            } else {
                prepare_and_run_with_stale_plan_retry(client, &sql, &params, &caching, |statement| {
                    let bound = &bound;
                    async move { client.execute(&statement, bound).await }
                })
                .await
                .map(|(_, affected)| affected)
            };
            // Disarm the drop-time cancel the instant the query finishes, successfully or not - a
            // guard still armed after this point only ever means the surrounding future was
            // dropped (Python-side cancellation) while the query was in flight, and a stray
            // CancelRequest could otherwise land on a later, unrelated query on this backend.
            let affected = cancel_guard.finish(result).map_err(to_pyerr)?;
            Ok(affected)
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
            let pooled_connection = PoolCheckout::get(&pool, &counters).await.map_err(to_pyerr)?;
            let mut cancel_guard = CancelQueryOnDrop::for_pooled_connection(pooled_connection, tls, cancel_target);
            let client = cancel_guard.connection();
            let bound = as_sql_params(&params);
            let result = async {
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
            // See execute()'s own identical guard above.
            let (columns, rows) = cancel_guard.finish(result).map_err(to_pyerr)?;
            Ok(PgResult::new(columns, rows))
        })
    }

    /// `fetch_all()` with the statement's column names first - `(names, rows)`, the names there for
    /// an empty result too.
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
            let pooled_connection = PoolCheckout::get(&pool, &counters).await.map_err(to_pyerr)?;
            let mut cancel_guard = CancelQueryOnDrop::for_pooled_connection(pooled_connection, tls, cancel_target);
            let client = cancel_guard.connection();
            let bound = as_sql_params(&params);
            let result = async {
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
            // See execute()'s own identical guard above.
            let (columns, rows) = cancel_guard.finish(result).map_err(to_pyerr)?;
            Ok(DescribedRows { columns, data: rows })
        })
    }

    fn fetch_one<'py>(&self, py: Python<'py>, sql: String, parameters: ParameterValues) -> PyResult<Bound<'py, PyAny>> {
        let params = parameters.0;
        let pool = self.pool.clone();
        let counters = self.counters.clone();
        let caching = self.statement_caching.clone();
        let tls = self.tls.clone();
        let cancel_target = self.cancel_target.clone();
        future_into_py(py, async move {
            let pooled_connection = PoolCheckout::get(&pool, &counters).await.map_err(to_pyerr)?;
            let mut cancel_guard = CancelQueryOnDrop::for_pooled_connection(pooled_connection, tls, cancel_target);
            let client = cancel_guard.connection();
            let bound = as_sql_params(&params);
            let result = async {
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
            // See execute()'s own identical guard above.
            let (columns, rows) = cancel_guard.finish(result).map_err(to_pyerr)?;
            Ok(FirstRow { columns, values: rows.into_iter().next() })
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
            let types = crate::pg::client::unified_param_types(&rows).map_err(to_pyerr)?;
            let pooled_connection = PoolCheckout::get(&pool, &counters).await.map_err(to_pyerr)?;
            // A batch cancelled from Python stops on the server too - its connection never goes back
            // to the pool busy with it (see execute()).
            let mut cancel_guard = CancelQueryOnDrop::for_pooled_connection(pooled_connection, tls, cancel_target);
            let outcome: Result<(), DriverError> = async {
                let client = cancel_guard.connection_mut();
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
            cancel_guard.finish(outcome).map_err(to_pyerr)
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
        let tls = self.tls.clone();
        let cancel_target = self.cancel_target.clone();
        future_into_py(py, async move {
            let pooled_connection = PoolCheckout::get(&pool, &counters).await.map_err(to_pyerr)?;
            // See execute(): a COPY cancelled from Python stops on the server too.
            let mut cancel_guard = CancelQueryOnDrop::for_pooled_connection(pooled_connection, tls, cancel_target);
            let outcome = crate::pg::copy::copy_in_records(
                cancel_guard.connection(),
                &table,
                schema.as_deref(),
                &columns,
                &column_types,
                records,
                &tags_comment,
            )
            .await;
            cancel_guard.finish(outcome).map_err(to_pyerr)
        })
    }

    /// Runs a (possibly multi-statement, `;`-separated) script in one round trip through the
    /// simple-query protocol.
    fn execute_script<'py>(&self, py: Python<'py>, sql: String) -> PyResult<Bound<'py, PyAny>> {
        let pool = self.pool.clone();
        let counters = self.counters.clone();
        let tls = self.tls.clone();
        let cancel_target = self.cancel_target.clone();
        future_into_py(py, async move {
            let pooled_connection = PoolCheckout::get(&pool, &counters).await.map_err(to_pyerr)?;
            // See execute(): a script cancelled from Python stops on the server too, its connection
            // never back in the pool still running it.
            let mut cancel_guard = CancelQueryOnDrop::for_pooled_connection(pooled_connection, tls, cancel_target);
            let outcome = cancel_guard.connection().batch_execute(&sql).await.map_err(DriverError::from);
            cancel_guard.finish(outcome).map_err(to_pyerr)
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
            Transaction::begin(client, isolation.as_deref(), caching, tls, cancel_target).map_err(to_pyerr)
        })
    }

    /// Starts a transaction on a connection already checked out via `acquire()` - so the caller
    /// can wait for a free pooled connection as an ordinary cancellable call. The connection is
    /// moved into the returned `Transaction`, whose BEGIN goes out with its first statement - so
    /// nothing is sent here and the call returns at once.
    fn begin_on(
        &self,
        pooled_connection: &Bound<'_, PooledConnection>,
        isolation: Option<&str>,
    ) -> PyResult<Transaction> {
        let inner = pooled_connection.borrow().inner.clone();
        // Nothing else holds a connection just checked out for a transaction.
        let client = inner.try_lock().map_err(|_| connection_busy())?.take().ok_or_else(connection_released)?;
        Transaction::begin(
            client,
            isolation,
            self.statement_caching.clone(),
            self.tls.clone(),
            self.cancel_target.clone(),
        )
        .map_err(to_pyerr)
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
        let (inner, connection_lost, pending_cancels) = {
            let pooled = pooled_connection.borrow();
            (pooled.inner.clone(), pooled.connection_lost.clone(), pooled.pending_cancels.clone())
        };
        future_into_py(pooled_connection.py(), async move {
            // A CancelRequest still on its way could stop the next caller's query.
            pending_cancels.wait_until_delivered().await;
            if let Some(connection) = inner.lock().await.take() {
                PooledConnection::release_connection(connection, &connection_lost);
            }
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
    let (client, mut connection) = match config.connect(tls.clone()).await {
        Ok(opened) => opened,
        Err(error) => return Err(ClosingConnect::get_reported_error(&config, tls, error).await),
    };
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

fn connection_busy() -> PyErr {
    pyo3::exceptions::PyRuntimeError::new_err("connection is in use by another call")
}

/// The parameter types of an `execute_many` batch, one per column for all rows: that of the first
/// value with a declared type (`Value::pg_type()`), widened when rows disagree (`widen_pg_type()`); a
/// batch mixing types that can't widen to one is refused. Adapted from yara-orm's
/// `unified_param_types` (MIT License).
pub(crate) fn unified_param_types(rows: &[Vec<Value>]) -> Result<Vec<Type>, DriverError> {
    let Some(first) = rows.first() else {
        return Ok(Vec::new());
    };
    let mut types: Vec<Option<Type>> = first.iter().map(Value::pg_type).collect();
    for row in &rows[1..] {
        for (index, value) in row.iter().enumerate().take(types.len()) {
            let Some(next) = value.pg_type() else {
                continue;
            };
            match &mut types[index] {
                slot @ None => *slot = Some(next),
                Some(current) if *current == next => {}
                Some(current) => match widen_pg_type(current, &next) {
                    Some(widened) => *current = widened,
                    None => {
                        return Err(DriverError::Query(format!(
                            "execute_many parameter {} mixes incompatible types across rows ({} vs {})",
                            index + 1,
                            current.name(),
                            next.name(),
                        )))
                    }
                },
            }
        }
    }
    Ok(types.into_iter().map(|postgres_type| postgres_type.unwrap_or_else(unspecified_type)).collect())
}

fn widen_pg_type(first_type: &Type, second_type: &Type) -> Option<Type> {
    match (first_type, second_type) {
        (&Type::TIMESTAMP, &Type::TIMESTAMPTZ) | (&Type::TIMESTAMPTZ, &Type::TIMESTAMP) => {
            return Some(Type::TIMESTAMPTZ)
        }
        (&Type::TEXT, &Type::UUID) | (&Type::UUID, &Type::TEXT) => return Some(Type::UUID),
        _ => {}
    }
    let numeric = |postgres_type: &Type| matches!(*postgres_type, Type::INT8 | Type::FLOAT8 | Type::NUMERIC);
    if numeric(first_type) && numeric(second_type) {
        return Some(Type::NUMERIC);
    }
    None
}

#[cfg(test)]
mod cancel_delivery_tests {
    use super::cancel_target::CloseAwaitingStream;
    use super::*;
    use std::sync::atomic::AtomicUsize;
    use tokio::net::TcpStream;

    fn runtime() -> tokio::runtime::Runtime {
        tokio::runtime::Builder::new_multi_thread().enable_all().build().expect("a test runtime")
    }

    #[test]
    fn wait_until_delivered_waits_for_every_pending_delivery() {
        runtime().block_on(async {
            let pending_cancels = PendingCancels::default();
            let finished = Arc::new(AtomicUsize::new(0));
            for delay_milliseconds in [30u64, 10, 20] {
                let finished = finished.clone();
                pending_cancels.add(tokio::spawn(async move {
                    tokio::time::sleep(Duration::from_millis(delay_milliseconds)).await;
                    finished.fetch_add(1, Ordering::SeqCst);
                }));
            }
            pending_cancels.wait_until_delivered().await;
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
            let server = tokio::spawn(async move {
                let (mut socket, _) = listener.accept().await.expect("an accepted connection");
                let mut request = Vec::new();
                tokio::io::AsyncReadExt::read_to_end(&mut socket, &mut request).await.expect("the request");
                tokio::time::sleep(Duration::from_millis(50)).await;
                server_closed_by_server.store(true, Ordering::SeqCst);
                drop(socket);
                request
            });
            let socket = TcpStream::connect(address).await.expect("a client connection");
            let mut stream = CloseAwaitingStream { socket, write_side_closed: false };
            tokio::io::AsyncWriteExt::write_all(&mut stream, b"cancel").await.expect("the write");
            tokio::io::AsyncWriteExt::shutdown(&mut stream).await.expect("the shutdown");
            assert!(server_closed.load(Ordering::SeqCst), "shutdown returned before the server closed the connection");
            assert_eq!(server.await.expect("the server task"), b"cancel");
        });
    }
}
