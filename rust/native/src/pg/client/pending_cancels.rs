use futures_util::future::{BoxFuture, Shared};
use futures_util::FutureExt;
use std::sync::{Arc, Mutex as StdMutex};
use tokio::task::JoinHandle;

/// The `CancelRequests` sent for statements abandoned mid-flight on one pinned connection. Every
/// later statement on that connection waits for their delivery first: a `CancelRequest` reaching
/// the server after the abandoned statement had already finished would otherwise cancel
/// whichever statement runs next. Once delivered, a cancel that finds the backend idle is
/// ignored by the server.
#[derive(Clone, Default)]
pub(crate) struct PendingCancels {
    deliveries: Arc<StdMutex<Vec<Shared<BoxFuture<'static, ()>>>>>,
}

impl PendingCancels {
    pub(super) fn add(&self, delivery: JoinHandle<()>) {
        self.deliveries.lock().unwrap_or_else(std::sync::PoisonError::into_inner).push(
            async move {
                let _ = delivery.await;
            }
            .boxed()
            .shared(),
        );
    }

    /// Whether no `CancelRequest` is still being delivered.
    pub(crate) fn is_empty(&self) -> bool {
        self.deliveries.lock().unwrap_or_else(std::sync::PoisonError::into_inner).is_empty()
    }

    /// Waits until every `CancelRequest` sent so far was delivered or given up on.
    /// The stored shared futures retain the deliveries when this waiter is cancelled.
    pub(crate) async fn wait_until_delivered(&self) {
        loop {
            let deliveries = self.deliveries.lock().unwrap_or_else(std::sync::PoisonError::into_inner).clone();
            if deliveries.is_empty() {
                return;
            }
            for delivery in deliveries {
                delivery.await;
            }
            self.deliveries
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .retain(|delivery| delivery.peek().is_none());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::future::{poll_fn, Future};
    use std::task::Poll;
    use tokio::sync::oneshot;

    #[test]
    fn cancelling_a_waiter_keeps_the_delivery_for_the_next_waiter() {
        tokio::runtime::Builder::new_current_thread().build().unwrap().block_on(async {
            let pending = PendingCancels::default();
            let (release, gate) = oneshot::channel();
            pending.add(tokio::spawn(async move {
                let _ = gate.await;
            }));
            let mut waiter = Box::pin(pending.wait_until_delivered());
            poll_fn(|context| {
                assert!(matches!(waiter.as_mut().poll(context), Poll::Pending));
                Poll::Ready(())
            })
            .await;
            drop(waiter);
            assert!(!pending.is_empty());
            let mut next = Box::pin(pending.wait_until_delivered());
            poll_fn(|context| {
                assert!(matches!(next.as_mut().poll(context), Poll::Pending));
                Poll::Ready(())
            })
            .await;
            release.send(()).unwrap();
            next.await;
            assert!(pending.is_empty());
        });
    }
}
