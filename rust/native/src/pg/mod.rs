//! `rust.native.pg` - an async PostgreSQL driver on `tokio-postgres`, `deadpool-postgres` (pooling)
//! and `tokio-postgres-rustls` (pure-Rust TLS), bridged to asyncio with `pyo3-async-runtimes`. Its design
//! was inspired by the Rust PostgreSQL backend of yara-orm (<https://github.com/vsdudakov/yara-orm>,
//! MIT License); the parts adapted from yara-orm's code are the TLS connection setup and the
//! `execute_many` parameter type unification (`client`), the base of `Value` (`value`) and
//! `DriverError` (`error`).

#[cfg_attr(not(unix), allow(dead_code))]
pub mod bounded_statement_tracker;
#[cfg(unix)]
pub mod client;
#[cfg(unix)]
pub mod closing_connect;
#[cfg(unix)]
pub(crate) mod connection_executor;
#[cfg(unix)]
pub(crate) mod connection_opening;
#[cfg(unix)]
pub mod copy;
pub(crate) mod decode_context;
#[cfg(unix)]
pub mod described_rows;
pub mod error;
#[cfg(unix)]
pub mod first_row;
#[cfg_attr(not(unix), allow(dead_code))]
pub mod managed_connection;
#[cfg(unix)]
pub mod one_round_trip;
pub(crate) mod parameter_encoding;
#[cfg(unix)]
pub mod parameter_value_rows;
#[cfg(unix)]
pub mod parameter_values;
#[cfg(unix)]
pub(crate) mod pending_statements;
pub mod result;
pub mod result_columns;
pub mod result_data;
pub mod row;
pub(crate) mod row_decoder;
#[cfg(unix)]
pub(crate) mod statement_lease;
// The driver's errors name the manager's - compiled everywhere, used on Unix alone.
#[cfg_attr(not(unix), allow(dead_code))]
pub mod timed_manager;
#[cfg(unix)]
pub(crate) mod tls;
#[cfg(unix)]
pub mod transaction;
pub mod types;
pub mod value;
pub(crate) mod wire_reader;

#[cfg(unix)]
use pyo3::prelude::*;

/// Fills the `pg` submodule - the driver is built on Unix alone (see the crate root).
#[cfg(unix)]
pub fn register(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<client::Client>()?;
    module.add_class::<client::PooledConnection>()?;
    module.add_class::<client::Listener>()?;
    module.add_class::<transaction::Transaction>()?;
    module.add_class::<transaction::RowStreamIterator>()?;
    module.add_class::<row::PgRow>()?;
    module.add_class::<result::PgResult>()?;
    module.add_function(wrap_pyfunction!(client::connect, module)?)?;
    error::register(py, module)?;
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
