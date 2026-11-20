//! Preserve server errors across a hostname's addresses, using tokio-postgres for each handshake.

use std::net::IpAddr;

use tokio_postgres::config::Host;
use tokio_postgres::tls::MakeTlsConnect;
use tokio_postgres::{Client, Config, Connection, Error, Socket};

use super::client::POSTGRES_DEFAULT_PORT;

pub(crate) async fn open_connection<T>(
    config: &Config,
    tls: T,
) -> Result<(Client, Connection<Socket, T::Stream>), Error>
where
    T: MakeTlsConnect<Socket> + Clone,
{
    // The library returns the last DNS address's error. A refused socket must not
    // hide authentication from a reachable address. Keep the original TLS host;
    // hostaddr selects only the socket address. Literal IPs need no extra lookup.
    let addresses = match config.get_hosts() {
        [Host::Tcp(host)] if host.parse::<IpAddr>().is_err() && config.get_hostaddrs().is_empty() => {
            let port = config.get_ports().first().copied().unwrap_or(POSTGRES_DEFAULT_PORT);
            tokio::net::lookup_host((host.as_str(), port))
                .await
                .ok()
                .map(|addresses| addresses.map(|address| address.ip()).collect::<Vec<_>>())
                .filter(|addresses| !addresses.is_empty())
        }
        _ => None,
    };
    if let Some(addresses) = addresses {
        let mut server_error = None;
        let mut transport_error = None;
        for address in addresses {
            let mut address_config = config.clone();
            address_config.hostaddr(address);
            match address_config.connect(tls.clone()).await {
                Ok(connection) => return Ok(connection),
                Err(error) if error.as_db_error().is_some() => server_error = Some(error),
                Err(error) => transport_error = Some(error),
            }
        }
        return Err(server_error.or(transport_error).expect("a nonempty address list was tried"));
    }
    config.connect(tls).await
}
