//! A removed transaction connection cannot return to the pool before finalization completes.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use super::schema_changes::SchemaChanges;
use crate::pg::client::PendingCancels;
use crate::pg::timed_manager::Object;

pub(super) struct ClosingTransaction {
    connection: Option<Object>,
    pending_cancels: PendingCancels,
    schema_changes: Arc<SchemaChanges>,
}

impl ClosingTransaction {
    pub(super) fn new(connection: Object, pending_cancels: PendingCancels, schema_changes: Arc<SchemaChanges>) -> Self {
        Self { connection: Some(connection), pending_cancels, schema_changes }
    }

    pub(super) fn connection(&self) -> &Object {
        self.connection.as_ref().expect("transaction finalization retains its connection")
    }

    /// Only a completed finalization may reuse the session. An abandoned one is disconnected.
    pub(super) fn release(mut self, poisoned: &AtomicBool) {
        self.schema_changes.publish();
        if let Some(connection) = self.connection.take() {
            if poisoned.load(Ordering::Acquire) {
                drop(Object::take(connection));
            } else {
                drop(connection);
            }
        }
    }
}

impl Drop for ClosingTransaction {
    fn drop(&mut self) {
        if let Some(connection) = self.connection.take() {
            self.schema_changes.publish();
            let connection = Object::take(connection);
            let pending_cancels = self.pending_cancels.clone();
            if pending_cancels.is_empty() {
                drop(connection);
            } else {
                pyo3_async_runtimes::tokio::get_runtime().spawn(async move {
                    pending_cancels.wait_until_delivered().await;
                    drop(connection);
                });
            }
        }
    }
}
