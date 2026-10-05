//! `ClosingConnect` - deadpool-postgres's way of opening a connection, its connection task ending
//! once the pool is closed for good: a connection a transaction still holds then is dropped, so the
//! server rolls the transaction back and ends the session - as asyncpg's `Pool.terminate()`.

use std::pin::pin;

use deadpool_postgres::Connect;
use futures_util::future::{select, BoxFuture};
use tokio::sync::watch;
use tokio::task::JoinHandle;
use tokio_postgres::config::Host;
use tokio_postgres::tls::{MakeTlsConnect, TlsConnect};
use tokio_postgres::{Client as PgClient, Config as PgConfig, Error, Socket};

use crate::pg::client::POSTGRES_DEFAULT_PORT;

pub(crate) struct ClosingConnect<T> {
    tls: T,
    /// True once the pool is closed for good - every connection task ends.
    closed: watch::Receiver<bool>,
}

impl<T> ClosingConnect<T> {
    pub(crate) fn new(tls: T, closed: watch::Receiver<bool>) -> Self {
        Self { tls, closed }
    }

    /// Waits until the pool is closed for good - forever when the pool is dropped without it.
    async fn wait_closed(mut closed: watch::Receiver<bool>) {
        if closed.wait_for(|is_closed| *is_closed).await.is_err() {
            std::future::pending::<()>().await;
        }
    }
}

impl<T> ClosingConnect<T>
where
    T: MakeTlsConnect<Socket> + Clone + Send + 'static,
    T::Stream: Send,
    T::TlsConnect: Send,
    <T::TlsConnect as TlsConnect<Socket>>::Future: Send,
{
    /// The error a failed connection reports: the one the server itself gave on one of the host's
    /// addresses when the last address tried gave none - tokio-postgres reports the last address,
    /// so a refused `::1` hid the password `127.0.0.1` rejected.
    pub(crate) async fn get_reported_error(pg_config: &PgConfig, tls: T, error: Error) -> Error {
        if error.as_db_error().is_some() || !pg_config.get_hostaddrs().is_empty() {
            return error;
        }
        let [Host::Tcp(host)] = pg_config.get_hosts() else {
            return error;
        };
        let port = pg_config.get_ports().first().copied().unwrap_or(POSTGRES_DEFAULT_PORT);
        let Ok(addresses) = tokio::net::lookup_host((host.as_str(), port)).await else {
            return error;
        };
        for address in addresses {
            let mut address_config = pg_config.clone();
            address_config.hostaddr(address.ip());
            if let Err(address_error) = address_config.connect(tls.clone()).await {
                if address_error.as_db_error().is_some() {
                    return address_error;
                }
            }
        }
        error
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
        Box::pin(async move {
            let (client, connection) = match pg_config.connect(tls.clone()).await {
                Ok(opened) => opened,
                Err(error) => return Err(Self::get_reported_error(&pg_config, tls, error).await),
            };
            let connection_task = tokio::spawn(async move {
                // Whichever ends first: the connection, or the pool - dropping the connection then.
                let _ = select(pin!(connection), pin!(Self::wait_closed(closed))).await;
            });
            Ok((client, connection_task))
        })
    }
}
