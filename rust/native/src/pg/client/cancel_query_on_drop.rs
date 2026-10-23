use crate::pg::error::DriverError;
use crate::pg::timed_manager::Object;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use super::{CancelTarget, PendingCancels, PgTls, PinnedConnectionState};

/// Sends a `CancelRequest` for the running statement when dropped before `finish()` - the Python
/// side cancelled the call. Dropping tokio-postgres's future only stops polling: the server would
/// keep running the statement and the connection would stay busy.
pub(crate) struct CancelQueryOnDrop {
    token: Option<tokio_postgres::CancelToken>,
    tls: PgTls,
    cancel_target: CancelTarget,
    completed: bool,
    /// The pooled connection the guarded query runs on - discarded instead of returned to the
    /// pool when the query is abandoned, since the `CancelRequest` sent for it can still arrive
    /// after the connection has been handed to another caller and cancel THAT caller's query.
    pooled_connection: Option<Object>,
    /// Set for a query on a transaction's pinned connection: the transaction discards that
    /// connection instead of returning it to the pool when the `CancelRequest`'s delivery could not
    /// be confirmed, or the connection turned out dead.
    connection_poisoned: Option<Arc<AtomicBool>>,
    /// Set for a query on a transaction's pinned connection: the transaction's later statements
    /// wait for the `CancelRequest` sent when this query is abandoned.
    pending_cancels: Option<PendingCancels>,
}

impl CancelQueryOnDrop {
    /// Guards a query on a pooled connection, taking ownership of that connection.
    pub(crate) fn for_pooled_connection(connection: Object, tls: PgTls, cancel_target: CancelTarget) -> Self {
        Self {
            token: Some(connection.cancel_token()),
            tls,
            cancel_target,
            completed: false,
            pooled_connection: Some(connection),
            connection_poisoned: None,
            pending_cancels: None,
        }
    }

    /// Guards a query on a transaction's pinned connection.
    pub(crate) fn for_pinned_connection(
        token: tokio_postgres::CancelToken,
        transaction_connection: &PinnedConnectionState,
    ) -> Self {
        Self::for_held_connection(
            token,
            transaction_connection.tls.clone(),
            transaction_connection.cancel_target.clone(),
            transaction_connection.connection_poisoned.clone(),
            transaction_connection.pending_cancels.clone(),
        )
    }

    /// Guards a query on a connection its caller holds - a transaction's, or one `Client::acquire()`
    /// checked out: the caller's later statements wait for the `CancelRequest` sent when this query is
    /// abandoned, and the connection is discarded when its delivery could not be confirmed.
    pub(crate) fn for_held_connection(
        token: tokio_postgres::CancelToken,
        tls: PgTls,
        cancel_target: CancelTarget,
        connection_poisoned: Arc<AtomicBool>,
        pending_cancels: PendingCancels,
    ) -> Self {
        Self {
            token: Some(token),
            tls,
            cancel_target,
            completed: false,
            pooled_connection: None,
            connection_poisoned: Some(connection_poisoned),
            pending_cancels: Some(pending_cancels),
        }
    }

    /// The pooled connection this guard owns.
    pub(crate) fn connection(&self) -> &Object {
        self.pooled_connection.as_ref().expect("only called on a guard built for a pooled connection")
    }

    /// The pooled connection this guard owns, to open a transaction on.
    pub(crate) fn connection_mut(&mut self) -> &mut Object {
        self.pooled_connection.as_mut().expect("only called on a guard built for a pooled connection")
    }

    /// Marks the guarded work completed and passes its outcome through - when it failed because
    /// the connection itself is gone, that connection is discarded (a pooled one detached from the
    /// pool, a pinned one marked poisoned) instead of being reused.
    pub(crate) fn finish<T>(&mut self, outcome: Result<T, DriverError>) -> Result<T, DriverError> {
        self.completed = true;
        if matches!(&outcome, Err(error) if error.is_connection_lost()) {
            if let Some(connection) = self.pooled_connection.take() {
                drop(Object::take(connection));
            }
            if let Some(connection_poisoned) = &self.connection_poisoned {
                connection_poisoned.store(true, Ordering::Release);
            }
        }
        outcome
    }
}

impl Drop for CancelQueryOnDrop {
    fn drop(&mut self) {
        if self.completed {
            return;
        }
        // Detached from the pool - never handed to another caller while the cancel below may
        // still be in flight - and closed only once the cancel is delivered: a pooler (PgBouncer)
        // forwards a cancel only while the client connection it names is still open.
        let detached_connection = self.pooled_connection.take().map(Object::take);
        let Some(token) = self.token.take() else { return };
        let tls = self.tls.clone();
        let cancel_target = self.cancel_target.clone();
        let connection_poisoned = self.connection_poisoned.clone();
        // Drop can't be async - the delivery runs on the ambient tokio runtime. This guard only
        // ever lives inside a future_into_py()-spawned task, which always runs on a tokio
        // runtime, so a runtime handle is always available here for tokio::spawn.
        let delivery = tokio::spawn(async move {
            // Best-effort: an undelivered request (network blip, server already gone) just
            // leaves the abandoned query to finish on its own - and the pinned connection is
            // never reused, since a late cancel could still hit its next user.
            if !cancel_target.send(token, tls).await {
                if let Some(connection_poisoned) = connection_poisoned {
                    connection_poisoned.store(true, Ordering::Release);
                }
            }
            drop(detached_connection);
        });
        if let Some(pending_cancels) = &self.pending_cancels {
            pending_cancels.add(delivery);
        }
    }
}
