//! `rust.native` - the native part of hare-orm: the `pg` PostgreSQL driver, the `pool` parts every
//! native driver shares, the batched asyncio bridge, the `rows` attribute,
//! JSON and value-format operations, the `sqlite_functions` hare registers on a SQLite connection, the
//! precise system `clock`.
//!
//! The asyncio bridge wakes its loop through a Unix socket pair watched by `loop.add_reader()`, so the
//! driver it serves - `pg`'s client and transactions, and `pool` - is built on Unix alone. Every other
//! part, `pg`'s value codecs among them, is built everywhere.

#[cfg(unix)]
mod asyncio;
mod clock;
mod json_string;
mod pg;
// The driver's errors name the pool's - compiled everywhere, registered on Unix alone.
#[cfg_attr(not(unix), allow(dead_code))]
mod pool;
mod python;
mod rows;
mod sqlite_functions;

use pyo3::prelude::*;
use pyo3::types::PyDict;

#[global_allocator]
static ALLOCATOR: mimalloc::MiMalloc = mimalloc::MiMalloc;

/// The most worker threads of the runtime the queries run on, where `TOKIO_WORKER_THREADS` names no
/// number - no more than the machine's cores. The queries are started by one Python thread: a few
/// workers keep up with it as one does, and while one decodes a large result the others still read
/// the other connections; many more spin while idle and take cores from Python and the database.
#[cfg(unix)]
const DEFAULT_WORKER_THREADS: usize = 4;

/// The environment variable of tokio's own that sets the number of worker threads.
#[cfg(unix)]
const WORKER_THREADS_VARIABLE: &str = "TOKIO_WORKER_THREADS";

/// Registers exports under their final module name before publishing the module. Both imports and
/// pickle must find an exported callable by the qualified name it receives during registration.
fn add_submodule<'py>(
    parent: &Bound<'py, PyModule>,
    name: &str,
    register: impl FnOnce(&Bound<'py, PyModule>) -> PyResult<()>,
) -> PyResult<()> {
    let py = parent.py();
    let qualified_name = format!("{}.{}", parent.name()?, name);
    let submodule = PyModule::new(py, &qualified_name)?;
    register(&submodule)?;
    parent.add_submodule(&submodule)?;
    let modules = py.import("sys")?.getattr("modules")?.cast_into::<PyDict>()?;
    modules.set_item(qualified_name, &submodule)?;
    Ok(())
}

#[pymodule]
fn native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    #[cfg(unix)]
    register_driver(module)?;
    add_submodule(module, "rows", rows::register)?;
    add_submodule(module, "sqlite_functions", sqlite_functions::register)?;
    add_submodule(module, "clock", clock::register)?;
    Ok(())
}

/// Adds the PostgreSQL driver - `pg` and `pool` - with the runtime its queries run on.
#[cfg(unix)]
fn register_driver(module: &Bound<'_, PyModule>) -> PyResult<()> {
    let mut runtime = tokio::runtime::Builder::new_multi_thread();
    runtime.enable_all();
    if std::env::var_os(WORKER_THREADS_VARIABLE).is_none() {
        let cores = std::thread::available_parallelism().map_or(1, std::num::NonZeroUsize::get);
        runtime.worker_threads(DEFAULT_WORKER_THREADS.min(cores));
    }
    pyo3_async_runtimes::tokio::init(runtime);
    add_submodule(module, "pg", pg::register)?;
    add_submodule(module, "pool", pool::register)?;
    Ok(())
}
