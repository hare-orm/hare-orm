//! `rust.native.pg` - an async PostgreSQL driver on `tokio-postgres`, `deadpool-postgres` (pooling)
//! and `tokio-postgres-rustls` (pure-Rust TLS), bridged to asyncio with `pyo3-async-runtimes`. Its design
//! was inspired by the Rust PostgreSQL backend of yara-orm (<https://github.com/vsdudakov/yara-orm>,
//! MIT License); the parts adapted from yara-orm's code are the TLS connection setup and the
//! `execute_many` parameter type unification (`client`), the base of `Value` (`value`) and
//! `DriverError` (`error`).

pub mod client;
pub mod completion;
pub mod copy;
pub mod error;
pub mod row;
pub mod statement_cache;
pub mod transaction;
pub mod types;
pub mod value;

use pyo3::prelude::*;

/// Fills the `pg` submodule.
pub fn register(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<client::Client>()?;
    module.add_class::<client::PooledConnection>()?;
    module.add_class::<client::Listener>()?;
    module.add_class::<transaction::Transaction>()?;
    module.add_class::<transaction::RowStreamIterator>()?;
    module.add_class::<row::PgRow>()?;
    module.add_function(wrap_pyfunction!(client::connect, module)?)?;
    error::register(py, module)?;
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
