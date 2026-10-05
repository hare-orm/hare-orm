//! `PoolStatistics` - what a pool of connections of a native driver has done: the connections taken, the waits for one
//! that ran out, how long the waits took, the connections opened and the failures to open one. The
//! Python client makes one per client and hands it to every pool it opens (`connect(statistics=...)`),
//! so a reconnect keeps it; the counters are atomic - read from the metrics exporter's thread too. It
//! has the methods of hare's own `PoolStatistics`, which counts for the drivers living in Python.

use std::sync::atomic::Ordering;
use std::sync::{Arc, PoisonError};
use std::time::Duration;

use pyo3::prelude::*;

use crate::pool::pool_counters::PoolCounters;

/// The Python face of a pool's counters.
#[pyclass(frozen, module = "rust.native.pool")]
pub struct PoolStatistics {
    pub(crate) counters: Arc<PoolCounters>,
}

#[pymethods]
impl PoolStatistics {
    /// `capacity`: how many of the latest durations of each type are kept for the readers.
    #[new]
    pub(crate) fn new(capacity: usize) -> Self {
        Self { counters: Arc::new(PoolCounters::new(capacity)) }
    }

    /// Counts a wait for a connection measured in Python - a transaction slot of the Python client.
    fn add_wait(&self, seconds: f64) {
        self.counters.add_wait(Duration::from_secs_f64(seconds.max(0.0)));
    }

    /// Counts a wait for a connection that ran out, noticed in Python.
    fn add_timeout(&self) {
        self.counters.add_timeout();
    }

    /// Counts a failure to open a pool, noticed in Python - its connects run before the pool exists.
    fn add_connect_failure(&self) {
        self.counters.add_connect_failure();
    }

    /// The connections taken, the timeouts, the time waited in all (seconds), the connections
    /// opened and the failures to open one.
    fn get_counts(&self) -> (u64, u64, f64, u64, u64) {
        let counters = &self.counters;
        #[allow(clippy::cast_precision_loss, reason = "seconds of waiting, far below 2^52 nanoseconds")]
        let wait_seconds = counters.acquire_wait_nanoseconds.load(Ordering::Relaxed) as f64 / 1e9;
        (
            counters.acquire_count.load(Ordering::Relaxed),
            counters.acquire_timeouts.load(Ordering::Relaxed),
            wait_seconds,
            counters.connect_count.load(Ordering::Relaxed),
            counters.connect_failures.load(Ordering::Relaxed),
        )
    }

    /// The waits for a connection after a reader's cursor - the new cursor, the waits in seconds and
    /// how many were already overwritten.
    fn get_waits_since(&self, cursor: u64) -> (u64, Vec<f64>, u64) {
        self.counters.waits.lock().unwrap_or_else(PoisonError::into_inner).get_since(cursor)
    }

    /// The connects after a reader's cursor - as `get_waits_since()`.
    fn get_connects_since(&self, cursor: u64) -> (u64, Vec<f64>, u64) {
        self.counters.connects.lock().unwrap_or_else(PoisonError::into_inner).get_since(cursor)
    }
}
