use std::pin::Pin;
use std::task::{ready, Context, Poll};
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::net::TcpStream;
use tokio_postgres::config::Host;
use tokio_postgres::tls::MakeTlsConnect;
use tokio_postgres::{CancelToken, NoTls};

use super::{PgTls, CANCEL_REQUEST_DELIVERY_TIMEOUT, POSTGRES_DEFAULT_PORT};

/// Where a `CancelRequest` is sent - the server the pool connects to.
#[derive(Clone)]
pub(crate) enum CancelTarget {
    /// One TCP host: the request goes over a socket this crate opens itself, so the delivery can
    /// wait until the server closes it - the server does so only after signalling the backend.
    Tcp { host: String, port: u16 },
    /// Anything else (a Unix socket, several hosts): tokio-postgres's own `cancel_query()`, which
    /// returns as soon as the request is written.
    Other,
}

impl CancelTarget {
    pub(crate) fn from_config(config: &tokio_postgres::Config) -> Self {
        let hosts = config.get_hosts();
        if hosts.len() != 1 || !config.get_hostaddrs().is_empty() {
            return CancelTarget::Other;
        }
        match &hosts[0] {
            Host::Tcp(host) => CancelTarget::Tcp {
                host: host.clone(),
                port: config.get_ports().first().copied().unwrap_or(POSTGRES_DEFAULT_PORT),
            },
            #[cfg(unix)]
            Host::Unix(_) => CancelTarget::Other,
        }
    }

    /// Sends the `CancelRequest` for `token` and waits for its delivery, bounded by
    /// `CANCEL_REQUEST_DELIVERY_TIMEOUT`. Returns whether the delivery was confirmed.
    pub(super) async fn send(self, token: CancelToken, tls: PgTls) -> bool {
        let delivery = async move {
            match self {
                CancelTarget::Tcp { host, port } => {
                    let Ok(socket) = TcpStream::connect((host.as_str(), port)).await else {
                        return false;
                    };
                    let socket = CloseAwaitingStream { socket, write_side_closed: false };
                    match tls {
                        PgTls::Disabled => token.cancel_query_raw(socket, NoTls).await.is_ok(),
                        PgTls::Rustls(mut connector) => {
                            match MakeTlsConnect::<CloseAwaitingStream>::make_tls_connect(&mut connector, &host) {
                                Ok(tls_connect) => token.cancel_query_raw(socket, tls_connect).await.is_ok(),
                                Err(_) => false,
                            }
                        }
                    }
                }
                CancelTarget::Other => {
                    // Written, but not confirmed delivered.
                    let _ = match tls {
                        PgTls::Disabled => token.cancel_query(NoTls).await,
                        PgTls::Rustls(connector) => token.cancel_query(connector).await,
                    };
                    false
                }
            }
        };
        tokio::time::timeout(CANCEL_REQUEST_DELIVERY_TIMEOUT, delivery).await.unwrap_or(false)
    }
}

/// The `CancelRequest` connection: `cancel_query_raw()` shuts it down right after writing the
/// request, and this shutdown only completes once the server has closed its end too.
pub(super) struct CloseAwaitingStream {
    pub(super) socket: TcpStream,
    pub(super) write_side_closed: bool,
}

impl AsyncRead for CloseAwaitingStream {
    fn poll_read(self: Pin<&mut Self>, cx: &mut Context<'_>, buffer: &mut ReadBuf<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.get_mut().socket).poll_read(cx, buffer)
    }
}

impl AsyncWrite for CloseAwaitingStream {
    fn poll_write(self: Pin<&mut Self>, cx: &mut Context<'_>, buffer: &[u8]) -> Poll<std::io::Result<usize>> {
        Pin::new(&mut self.get_mut().socket).poll_write(cx, buffer)
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Pin::new(&mut self.get_mut().socket).poll_flush(cx)
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        let this = self.get_mut();
        if !this.write_side_closed {
            ready!(Pin::new(&mut this.socket).poll_shutdown(cx))?;
            this.write_side_closed = true;
        }
        let mut scratch = [0u8; 64];
        loop {
            let mut buffer = ReadBuf::new(&mut scratch);
            match ready!(Pin::new(&mut this.socket).poll_read(cx, &mut buffer)) {
                Ok(()) if buffer.filled().is_empty() => return Poll::Ready(Ok(())),
                Ok(()) => {}
                Err(error) => return Poll::Ready(Err(error)),
            }
        }
    }
}
