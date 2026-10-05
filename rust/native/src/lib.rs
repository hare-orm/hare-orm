//! `rust.native` - the native part of hare-orm: the `pg` PostgreSQL driver, the `pool` parts every
//! native driver shares (the asyncio bridge, `asyncio`, stays inside the crate), the `rows` readers
//! and writers of model rows, the `sqlite_functions` hare registers on a SQLite connection, the
//! precise system `clock`.

mod asyncio;
mod clock;
mod codecs;
mod pg;
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
const DEFAULT_WORKER_THREADS: usize = 4;

/// The environment variable of tokio's own that sets the number of worker threads.
const WORKER_THREADS_VARIABLE: &str = "TOKIO_WORKER_THREADS";

/// Adds `submodule` to `parent` and to `sys.modules`, so both `from rust.native import pg` and
/// `import rust.native.pg` find it.
fn add_submodule(parent: &Bound<'_, PyModule>, submodule: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = parent.py();
    parent.add_submodule(submodule)?;
    let qualified_name = format!("{}.{}", parent.name()?, submodule.name()?);
    submodule.setattr("__name__", &qualified_name)?;
    let modules = py.import("sys")?.getattr("modules")?.cast_into::<PyDict>()?;
    modules.set_item(qualified_name, submodule)?;
    Ok(())
}

#[pymodule]
fn native(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    let mut runtime = tokio::runtime::Builder::new_multi_thread();
    runtime.enable_all();
    if std::env::var_os(WORKER_THREADS_VARIABLE).is_none() {
        let cores = std::thread::available_parallelism().map_or(1, std::num::NonZeroUsize::get);
        runtime.worker_threads(DEFAULT_WORKER_THREADS.min(cores));
    }
    pyo3_async_runtimes::tokio::init(runtime);
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    let pg_module = PyModule::new(py, "pg")?;
    pg::register(py, &pg_module)?;
    add_submodule(module, &pg_module)?;
    let pool_module = PyModule::new(py, "pool")?;
    pool::register(&pool_module)?;
    add_submodule(module, &pool_module)?;
    let rows_module = PyModule::new(py, "rows")?;
    rows::register(&rows_module)?;
    add_submodule(module, &rows_module)?;
    let sqlite_functions_module = PyModule::new(py, "sqlite_functions")?;
    sqlite_functions::register(&sqlite_functions_module)?;
    add_submodule(module, &sqlite_functions_module)?;
    let clock_module = PyModule::new(py, "clock")?;
    clock::register(&clock_module)?;
    add_submodule(module, &clock_module)?;
    Ok(())
}
