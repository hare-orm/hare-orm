//! `PoolCheckout` - the one way a native driver takes a connection from its deadpool pool: counted,
//! the wait measured while hare's pool metrics are enabled, and a failure told apart - the wait
//! running out, a new connection failing to open, anything else (`PoolCheckoutError`).

use std::fmt::Display;
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};

use deadpool::managed::{Manager, Object, Pool, PoolError};

use crate::pool::backend_error_code::BackendErrorCode;
use crate::pool::pool_checkout_error::PoolCheckoutError;
use crate::pool::pool_counters::PoolCounters;
use crate::pool::pool_metrics_switch::POOL_METRICS_ENABLED;

pub(crate) struct PoolCheckout;

impl PoolCheckout {
    /// Takes a connection from `pool`, waiting for one as the pool's `wait` timeout allows.
    pub(crate) async fn get<M: Manager>(pool: &Pool<M>, counters: &PoolCounters) -> Result<Object<M>, PoolCheckoutError>
    where
        M::Error: Display + BackendErrorCode,
    {
        let started = POOL_METRICS_ENABLED.load(Ordering::Relaxed).then(Instant::now);
        match pool.get().await {
            Ok(connection) => {
                PoolCheckout::count_taken(counters, started.map(|started| started.elapsed()));
                Ok(connection)
            }
            Err(error) => Err(PoolCheckout::get_error(counters, error)),
        }
    }

    /// Counts a connection taken - with the wait, when it was measured.
    pub(crate) fn count_taken(counters: &PoolCounters, wait: Option<Duration>) {
        counters.add_acquire();
        if let Some(wait) = wait {
            counters.add_wait(wait);
        }
    }

    /// The error of a failed checkout, counted.
    pub(crate) fn get_error<E: Display + BackendErrorCode>(
        counters: &PoolCounters,
        error: PoolError<E>,
    ) -> PoolCheckoutError {
        match error {
            PoolError::Timeout(_) => {
                counters.add_timeout();
                PoolCheckoutError::Timeout(error.to_string())
            }
            PoolError::Backend(ref backend_error) => {
                counters.add_connect_failure();
                let code = backend_error.get_code();
                PoolCheckoutError::ConnectFailed(error.to_string(), code)
            }
            other => PoolCheckoutError::Other(other.to_string()),
        }
    }
}
