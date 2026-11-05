//! Abandoning any statement schedules SQL cancellation without releasing its connection lease.

use super::{CancelTarget, PendingCancels, PinnedConnectionState};
use crate::pg::error::DriverError;
use crate::pg::tls::PgTls;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

pub(crate) struct CancelQueryOnDrop {
    token: Option<tokio_postgres::CancelToken>,
    tls: PgTls,
    cancel_target: CancelTarget,
    completed: bool,
    connection_poisoned: Arc<AtomicBool>,
    pending_cancels: PendingCancels,
}

impl CancelQueryOnDrop {
    pub(crate) fn new(token: tokio_postgres::CancelToken, state: &PinnedConnectionState) -> Self {
        Self {
            token: Some(token),
            tls: state.tls.clone(),
            cancel_target: state.cancel_target.clone(),
            completed: false,
            connection_poisoned: state.connection_poisoned.clone(),
            pending_cancels: state.pending_cancels.clone(),
        }
    }

    pub(crate) fn finish<T>(&mut self, outcome: Result<T, DriverError>) -> Result<T, DriverError> {
        self.completed = true;
        if matches!(&outcome, Err(error) if error.is_connection_lost()) {
            self.connection_poisoned.store(true, Ordering::Release);
        }
        outcome
    }
}

impl Drop for CancelQueryOnDrop {
    fn drop(&mut self) {
        if self.completed {
            return;
        }
        let Some(token) = self.token.take() else {
            return;
        };
        let tls = self.tls.clone();
        let cancel_target = self.cancel_target.clone();
        let poisoned = self.connection_poisoned.clone();
        // Mark unsafe before spawning: a task panic must not make the connection reusable.
        let already_poisoned = poisoned.swap(true, Ordering::AcqRel);
        let delivery = tokio::spawn(async move {
            if cancel_target.send(token, tls).await && !already_poisoned {
                poisoned.store(false, Ordering::Release);
            }
        });
        self.pending_cancels.add(delivery);
    }
}
