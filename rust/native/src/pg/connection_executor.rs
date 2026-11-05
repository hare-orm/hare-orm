//! Serialized access to a physical connection, including pending cancellation delivery.

use crate::pg::tls::PgTls;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use tokio::sync::Mutex;

use crate::pg::client::{CancelTarget, PendingCancels, PinnedConnectionState, StatementCaching};
use crate::pg::error::DriverError;
use crate::pg::statement_lease::PgStatementLease;
use crate::pg::timed_manager::Object;

#[derive(Clone)]
pub(crate) struct PgConnectionExecutor {
    pub(crate) connection: Arc<Mutex<Option<Object>>>,
    pub(crate) state: PinnedConnectionState,
    pub(crate) caching: StatementCaching,
    needs_drain: Arc<AtomicBool>,
}

impl PgConnectionExecutor {
    pub(crate) fn new(connection: Object, caching: StatementCaching, tls: PgTls, cancel_target: CancelTarget) -> Self {
        Self {
            connection: Arc::new(Mutex::new(Some(connection))),
            state: PinnedConnectionState {
                tls,
                cancel_target,
                connection_poisoned: Arc::new(AtomicBool::new(false)),
                pending_cancels: PendingCancels::default(),
            },
            caching,
            needs_drain: Arc::new(AtomicBool::new(false)),
        }
    }

    /// None means the connection was transferred or released. The caller selects its public error.
    /// Holding the connection lock while draining cancellations prevents a late cancel from reaching
    /// another statement. Cancelling this wait leaves both the connection and deliveries retained.
    pub(crate) async fn acquire(&self) -> Result<Option<PgStatementLease<'_>>, DriverError> {
        let connection = self.connection.lock().await;
        if connection.is_none() {
            return Ok(None);
        }
        self.wait_for_idle(connection.as_ref().expect("retained connection")).await?;
        self.caching.refresh(connection.as_ref().expect("retained connection"));
        Ok(Some(PgStatementLease::new(connection, &self.state)))
    }

    /// No connection is removed from its holder before all cancellable waits finish.
    pub(crate) async fn release(&self) {
        let mut guard = self.connection.lock().await;
        if let Some(connection) = guard.as_ref() {
            // A failed drain poisons the session; release then disconnects it.
            let _ = self.wait_for_idle(connection).await;
        }
        if let Some(connection) = guard.take() {
            if self.state.connection_poisoned.load(Ordering::Acquire) {
                drop(Object::take(connection));
            } else {
                drop(connection);
            }
        }
    }

    /// A response after the abandoned command confirms that its protocol stream was drained.
    /// The flag survives cancellation of this waiter, so another call resumes the barrier.
    async fn wait_for_idle(&self, connection: &Object) -> Result<(), DriverError> {
        if !self.state.pending_cancels.is_empty() {
            self.needs_drain.store(true, Ordering::Release);
        }
        self.state.pending_cancels.wait_until_delivered().await;
        if self.state.connection_poisoned.load(Ordering::Acquire) {
            return Err(DriverError::Connection("connection is no longer reusable".to_string()));
        }
        if self.needs_drain.load(Ordering::Acquire) {
            if let Err(error) = connection.batch_execute("").await {
                self.state.connection_poisoned.store(true, Ordering::Release);
                return Err(error.into());
            }
            self.needs_drain.store(false, Ordering::Release);
        }
        Ok(())
    }

    pub(crate) fn release_in_background(&self) {
        let executor = self.clone();
        pyo3_async_runtimes::tokio::get_runtime().spawn(async move {
            executor.release().await;
        });
    }
}
