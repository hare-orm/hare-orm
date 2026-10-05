//! `PoolCounters` - the atomic counters of one pool and its latest durations, shared by the pool's
//! checkouts, its connection manager and the Python `PoolStatistics`.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, PoisonError};
use std::time::Duration;

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
        }
    }

    pub(crate) fn add_acquire(&self) {
        self.acquire_count.fetch_add(1, Ordering::Relaxed);
    }

    pub(crate) fn add_wait(&self, wait: Duration) {
        self.acquire_wait_nanoseconds.fetch_add(u64::try_from(wait.as_nanos()).unwrap_or(u64::MAX), Ordering::Relaxed);
        self.waits.lock().unwrap_or_else(PoisonError::into_inner).add(wait.as_secs_f64());
    }

    pub(crate) fn add_timeout(&self) {
        self.acquire_timeouts.fetch_add(1, Ordering::Relaxed);
    }

    pub(crate) fn add_connect(&self, duration: Duration) {
        self.connect_count.fetch_add(1, Ordering::Relaxed);
        self.connects.lock().unwrap_or_else(PoisonError::into_inner).add(duration.as_secs_f64());
    }

    pub(crate) fn add_connect_failure(&self) {
        self.connect_failures.fetch_add(1, Ordering::Relaxed);
    }
}
