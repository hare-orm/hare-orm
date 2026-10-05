//! `rust.native.pool` - the connection pool parts every native driver shares: its counters and
//! the Python `PoolStatistics`, the metrics switch and the counted checkout of a deadpool pool.

pub mod backend_error_code;
pub mod duration_records;
pub mod pool_checkout;
pub mod pool_checkout_error;
pub mod pool_counters;
pub mod pool_metrics_switch;
pub mod pool_statistics;

use pyo3::prelude::*;

/// Fills the `pool` submodule.
pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<pool_statistics::PoolStatistics>()?;
    module.add_function(wrap_pyfunction!(pool_metrics_switch::set_pool_metrics_enabled, module)?)?;
    Ok(())
}
