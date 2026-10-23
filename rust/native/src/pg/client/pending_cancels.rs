use std::sync::{Arc, Mutex as StdMutex};
use tokio::task::JoinHandle;

/// The `CancelRequests` sent for statements abandoned mid-flight on one pinned connection. Every
/// later statement on that connection waits for their delivery first: a `CancelRequest` reaching
/// the server after the abandoned statement had already finished would otherwise cancel
/// whichever statement runs next. Once delivered, a cancel that finds the backend idle is
/// ignored by the server.
#[derive(Clone, Default)]
pub(crate) struct PendingCancels {
    deliveries: Arc<StdMutex<Vec<JoinHandle<()>>>>,
}

impl PendingCancels {
    pub(super) fn add(&self, delivery: JoinHandle<()>) {
        self.deliveries.lock().unwrap_or_else(std::sync::PoisonError::into_inner).push(delivery);
    }

    /// Whether no `CancelRequest` is still being delivered.
    pub(crate) fn is_empty(&self) -> bool {
        self.deliveries.lock().unwrap_or_else(std::sync::PoisonError::into_inner).is_empty()
    }

    /// Waits until every `CancelRequest` sent so far was delivered or given up on.
    pub(crate) async fn wait_until_delivered(&self) {
        loop {
            let deliveries =
                std::mem::take(&mut *self.deliveries.lock().unwrap_or_else(std::sync::PoisonError::into_inner));
            if deliveries.is_empty() {
                return;
            }
            for delivery in deliveries {
                let _ = delivery.await;
            }
        }
    }
}
