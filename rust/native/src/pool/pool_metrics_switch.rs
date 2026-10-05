//! The switch of hare's pool metrics for every native pool: off, taking a connection reads no clock.

use std::sync::atomic::{AtomicBool, Ordering};

use pyo3::prelude::*;

/// Whether the pools measure how long each wait for a connection takes - set from hare's
/// `PoolMetrics`. Off by default: taking a connection then reads no clock.
pub(crate) static POOL_METRICS_ENABLED: AtomicBool = AtomicBool::new(false);

/// Starts or stops measuring the waits for a connection - hare's `PoolMetrics` calls it.
#[pyfunction]
pub fn set_pool_metrics_enabled(enabled: bool) {
    POOL_METRICS_ENABLED.store(enabled, Ordering::Relaxed);
}
