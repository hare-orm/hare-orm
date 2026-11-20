//! Catalog metadata must be refreshed again when a transaction changes its visibility.

use std::sync::atomic::{AtomicBool, Ordering};

use crate::pg::client::StatementCaching;

pub(super) struct SchemaChanges {
    caching: StatementCaching,
    touched: AtomicBool,
}

impl SchemaChanges {
    pub(super) fn new(caching: StatementCaching) -> Self {
        Self { caching, touched: AtomicBool::new(false) }
    }

    /// Refresh now and retain the obligation to publish later commit/rollback boundaries.
    pub(super) fn invalidate(&self) {
        self.touched.store(true, Ordering::Release);
        self.caching.invalidate();
    }

    /// A savepoint rollback does not clear the outer transaction's publication obligation.
    pub(super) fn publish(&self) {
        if self.touched.load(Ordering::Acquire) {
            self.caching.invalidate();
        }
    }
}
