//! Transaction control queued until the first wire operation under the connection lease.

use std::future::Future;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use crate::pg::timed_manager::Object;

#[derive(Default)]
pub(crate) struct PendingStatements {
    statements: Mutex<Vec<String>>,
    begin_pending: AtomicBool,
    control_in_flight: AtomicBool,
}

impl PendingStatements {
    pub(crate) fn begin(&self, sql: String) {
        self.push(sql);
        self.begin_pending.store(true, Ordering::Release);
    }

    pub(crate) fn push(&self, sql: String) {
        self.statements.lock().unwrap_or_else(std::sync::PoisonError::into_inner).push(sql);
    }

    /// True until a wire operation takes BEGIN. Empty transactions need no ending SQL.
    pub(crate) fn has_begin(&self) -> bool {
        self.begin_pending.load(Ordering::Acquire)
    }

    /// An interrupted or rejected BEGIN cannot be treated as an established transaction.
    pub(crate) fn control_in_flight(&self) -> bool {
        self.control_in_flight.load(Ordering::Acquire)
    }

    fn take(&self) -> Option<String> {
        let mut statements = self.statements.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
        if statements.is_empty() {
            return None;
        }
        let sql = std::mem::take(&mut *statements).join("; ");
        self.begin_pending.store(false, Ordering::Release);
        Some(sql)
    }

    /// Confirm control before polling the operation, under one statement lease.
    /// A failed control leaves the guard armed, so the lease disconnects the session:
    /// a caller catching the error cannot issue a later write outside its transaction.
    pub(crate) async fn run<T, E>(
        pending: Option<&Self>,
        client: &Object,
        operation: impl Future<Output = Result<T, E>>,
    ) -> Result<T, E>
    where
        E: From<tokio_postgres::Error>,
    {
        match pending.and_then(Self::take) {
            None => operation.await,
            Some(sql) => {
                let pending = pending.expect("pending control statements");
                pending.control_in_flight.store(true, Ordering::Release);
                client.batch_execute(&sql).await.map_err(E::from)?;
                pending.control_in_flight.store(false, Ordering::Release);
                operation.await
            }
        }
    }
}
