//! `rust.native` - the native part of hare-orm: the `pg` PostgreSQL driver and the `rows` readers
//! and writers of model rows, the `sqlite_functions` hare registers on a SQLite connection.

mod codecs;
mod pg;
mod python;
mod rows;
mod sqlite_functions;

use pyo3::prelude::*;
use pyo3::types::PyDict;

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
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    let pg_module = PyModule::new(py, "pg")?;
    pg::register(py, &pg_module)?;
    add_submodule(module, &pg_module)?;
    let rows_module = PyModule::new(py, "rows")?;
    rows::register(&rows_module)?;
    add_submodule(module, &rows_module)?;
    let sqlite_functions_module = PyModule::new(py, "sqlite_functions")?;
    sqlite_functions::register(&sqlite_functions_module)?;
    add_submodule(module, &sqlite_functions_module)?;
    Ok(())
}
