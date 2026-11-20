//! `ClosingConnect` - deadpool-postgres's way of opening a connection, its connection task ending
//! once the pool is closed for good: a connection a transaction still holds then is dropped, so the
//! server rolls the transaction back and ends the session - as asyncpg's `Pool.terminate()`.

use std::pin::pin;

use deadpool_postgres::Connect;
use futures_util::future::{select, BoxFuture};
use tokio::sync::watch;
use tokio::task::JoinHandle;
use tokio_postgres::tls::{MakeTlsConnect, TlsConnect};
use tokio_postgres::{Client as PgClient, Config as PgConfig, Error, Socket};
use tokio_util::task::TaskTracker;

use super::connection_opening::open_connection;

pub(crate) struct ClosingConnect<T> {
    tls: T,
    /// True once the pool is closed for good - every connection task ends.
    closed: watch::Receiver<bool>,
    connection_tasks: TaskTracker,
}

impl<T> ClosingConnect<T> {
    pub(crate) fn new(tls: T, closed: watch::Receiver<bool>, connection_tasks: TaskTracker) -> Self {
        Self { tls, closed, connection_tasks }
    }

    /// Waits until the pool is closed for good - forever when the pool is dropped without it.
    async fn wait_closed(mut closed: watch::Receiver<bool>) {
        if closed.wait_for(|is_closed| *is_closed).await.is_err() {
            std::future::pending::<()>().await;
        }
    }
}

impl<T> Connect for ClosingConnect<T>
where
    T: MakeTlsConnect<Socket> + Clone + Sync + Send + 'static,
    T::Stream: Sync + Send,
    T::TlsConnect: Sync + Send,
    <T::TlsConnect as TlsConnect<Socket>>::Future: Send,
{
    fn connect(&self, pg_config: &PgConfig) -> BoxFuture<'_, Result<(PgClient, JoinHandle<()>), Error>> {
        let tls = self.tls.clone();
        let pg_config = pg_config.clone();
        let closed = self.closed.clone();
        let connection_tasks = self.connection_tasks.clone();
        Box::pin(async move {
            let (client, connection) = open_connection(&pg_config, tls).await?;
            let connection_task = connection_tasks.spawn(async move {
                // Poll shutdown first: a handshake finishing after close must never drive queued SQL.
                let _ = select(pin!(Self::wait_closed(closed)), pin!(connection)).await;
            });
            Ok((client, connection_task))
        })
    }
}
