//! `TimedManager` - deadpool-postgres's `Manager`, counting every connection it opens with the time
//! opening it took (`PoolCounters`). deadpool has no hook before a connection is created, so the
//! manager itself is wrapped. `Pool` and `Object` are the pool and the pooled connection of this
//! manager - the names deadpool-postgres gives its own. The wrapped manager is replaced by one with
//! new credentials (`Client.set_password()`): the connections opened next use them, the open ones
//! stay.

use std::sync::{Arc, PoisonError, RwLock};
use std::time::Instant;

use deadpool::managed::{Metrics, RecycleResult};
use deadpool_postgres::Manager;

use crate::pg::managed_connection::ManagedConnection;

use crate::pool::pool_counters::PoolCounters;

pub(crate) type Pool = deadpool::managed::Pool<TimedManager>;
pub(crate) type Object = deadpool::managed::Object<TimedManager>;
pub(crate) type PoolError = deadpool::managed::PoolError<tokio_postgres::Error>;

pub(crate) struct TimedManager {
    inner: RwLock<Arc<Manager>>,
    pub(crate) counters: Arc<PoolCounters>,
    statement_cache_size: Option<usize>,
}

impl TimedManager {
    pub(crate) fn new(inner: Manager, counters: Arc<PoolCounters>, statement_cache_size: Option<usize>) -> Self {
        Self { inner: RwLock::new(Arc::new(inner)), counters, statement_cache_size }
    }

    /// The manager the next connection is opened with.
    fn current(&self) -> Arc<Manager> {
        self.inner.read().unwrap_or_else(PoisonError::into_inner).clone()
    }

    /// Opens the connections from now on with `manager`.
    pub(crate) fn replace(&self, manager: Manager) {
        *self.inner.write().unwrap_or_else(PoisonError::into_inner) = Arc::new(manager);
    }
}

impl deadpool::managed::Manager for TimedManager {
    type Type = ManagedConnection;
    type Error = tokio_postgres::Error;

    async fn create(&self) -> Result<ManagedConnection, tokio_postgres::Error> {
        let started = Instant::now();
        let client = self.current().create().await?;
        self.counters.add_connect(started.elapsed());
        Ok(ManagedConnection::new(client, self.statement_cache_size))
    }

    async fn recycle(&self, client: &mut ManagedConnection, metrics: &Metrics) -> RecycleResult<tokio_postgres::Error> {
        self.current().recycle(&mut client.client, metrics).await
    }

    fn detach(&self, client: &mut ManagedConnection) {
        self.current().detach(&mut client.client);
    }
}
