//! `PoolCounters` - the atomic counters of one pool and its latest durations, shared by the pool's
//! checkouts, its connection manager and the Python `PoolStatistics`.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use pyo3::prelude::*;

use crate::pool::duration_records::DurationRecords;

/// The counters of a pool, shared by the pool's checkouts, its manager and the Python object.
pub(crate) struct PoolCounters {
    pub(crate) acquire_count: AtomicU64,
    pub(crate) acquire_timeouts: AtomicU64,
    pub(crate) acquire_wait_nanoseconds: AtomicU64,
    pub(crate) connect_count: AtomicU64,
    pub(crate) connect_failures: AtomicU64,
    pub(crate) waits: Mutex<DurationRecords>,
    pub(crate) connects: Mutex<DurationRecords>,
    pub(crate) recorder: Mutex<Option<Arc<Py<PyAny>>>>,
}

impl PoolCounters {
    pub(crate) fn new(capacity: usize) -> Self {
        Self {
            acquire_count: AtomicU64::new(0),
            acquire_timeouts: AtomicU64::new(0),
            acquire_wait_nanoseconds: AtomicU64::new(0),
            connect_count: AtomicU64::new(0),
            connect_failures: AtomicU64::new(0),
            waits: Mutex::new(DurationRecords::new(capacity)),
            connects: Mutex::new(DurationRecords::new(capacity)),
            recorder: Mutex::new(None),
        }
    }

    pub(crate) fn add_acquire(&self) {
        self.acquire_count.fetch_add(1, Ordering::Relaxed);
    }

    pub(crate) fn add_wait(&self, wait: Duration) {
        self.acquire_wait_nanoseconds.fetch_add(u64::try_from(wait.as_nanos()).unwrap_or(u64::MAX), Ordering::Relaxed);
        self.waits.lock().unwrap_or_else(PoisonError::into_inner).add(wait.as_secs_f64());
        self.record("wait", wait.as_secs_f64());
    }

    pub(crate) fn add_timeout(&self) {
        self.acquire_timeouts.fetch_add(1, Ordering::Relaxed);
        self.record("timeout", 1.0);
    }

    pub(crate) fn add_connect(&self, duration: Duration) {
        self.connect_count.fetch_add(1, Ordering::Relaxed);
        self.connects.lock().unwrap_or_else(PoisonError::into_inner).add(duration.as_secs_f64());
        self.record("connect", duration.as_secs_f64());
    }

    pub(crate) fn add_connect_failure(&self) {
        self.connect_failures.fetch_add(1, Ordering::Relaxed);
        self.record("connect_failure", 1.0);
    }

    /// Dispatch once at completion, after releasing counter/sample locks. No Python call while disabled.
    fn record(&self, event: &str, value: f64) {
        let recorder = self.recorder.lock().unwrap_or_else(PoisonError::into_inner).clone();
        if let Some(recorder) = recorder {
            Python::attach(|py| {
                if let Err(error) = recorder.call1(py, (event, value)) {
                    error.write_unraisable(py, None);
                }
            });
        }
    }
}
