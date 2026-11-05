//! A physical connection and the statement metadata that must be released with it.

use std::ops::{Deref, DerefMut};
use std::sync::atomic::AtomicU64;
use std::sync::Mutex;

use deadpool_postgres::ClientWrapper;

use crate::pg::bounded_statement_tracker::BoundedStatementTracker;

pub(crate) struct ManagedConnection {
    pub(crate) client: ClientWrapper,
    pub(crate) statement_order: Option<Mutex<BoundedStatementTracker>>,
    pub(crate) schema_generation: AtomicU64,
}

impl ManagedConnection {
    pub(crate) fn new(client: ClientWrapper, statement_cache_size: Option<usize>) -> Self {
        let statement_order =
            statement_cache_size.filter(|size| *size > 0).map(|size| Mutex::new(BoundedStatementTracker::new(size)));
        Self { client, statement_order, schema_generation: AtomicU64::new(0) }
    }
}

impl Deref for ManagedConnection {
    type Target = ClientWrapper;

    fn deref(&self) -> &Self::Target {
        &self.client
    }
}

impl DerefMut for ManagedConnection {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.client
    }
}
