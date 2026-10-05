use crate::pool::pool_checkout_error::PoolCheckoutError;
use std::error::Error as StdError;
use thiserror::Error;

use super::{ServerError, ServerErrorClass};

#[derive(Debug, Error)]
pub enum DriverError {
    #[error("configuration error: {0}")]
    Config(String),
    #[error("connection error: {0}")]
    Connection(String),
    /// Waiting for a pool connection ran out of the pool's wait timeout.
    #[error("pool timeout: {0}")]
    PoolTimeout(String),
    /// The pool was closed before a connection was taken from it.
    #[error("connection error: {0}")]
    PoolClosed(String),
    /// Opening a new pool connection failed - with the SQLSTATE of the server's refusal, when the
    /// server refused it (a rejected password: class 28).
    #[error("connection error: {0}")]
    ConnectFailed(String, Option<String>),
    #[error("query error: {0}")]
    Query(String),
    /// An error the server reported.
    #[error("{}", .0.message)]
    Server(Box<ServerError>),
    /// A value with no conversion between Python and PostgreSQL (an unsupported bind value, a result
    /// value out of the Python type's range, ...).
    #[error("type conversion error: {0}")]
    Conversion(String),
}

impl DriverError {
    /// A failed pool checkout. A server-reported error keeps its class - a rejected password (SQLSTATE
    /// class 28) or a missing database (class 3D) is a configuration problem no retry fixes - anything
    /// else is a connection error.
    pub fn from_pool_error(error: crate::pg::timed_manager::PoolError) -> Self {
        match error {
            crate::pg::timed_manager::PoolError::Backend(backend_error) if backend_error.as_db_error().is_some() => {
                DriverError::from(backend_error)
            }
            crate::pg::timed_manager::PoolError::Closed => {
                DriverError::PoolClosed(crate::pg::timed_manager::PoolError::Closed.to_string())
            }
            other => DriverError::Connection(Self::message_with_causes(&other)),
        }
    }

    /// The error's text followed by every cause of its `source()` chain not already in it - the text
    /// names what failed, the chain says why ("connection refused", "timed out").
    fn message_with_causes(error: &dyn StdError) -> String {
        let mut message = error.to_string();
        let mut source = error.source();
        while let Some(cause) = source {
            let cause_text = cause.to_string();
            if !message.contains(&cause_text) {
                message.push_str(": ");
                message.push_str(&cause_text);
            }
            source = cause.source();
        }
        message
    }

    /// Whether the connection itself is gone - it must never go back into the pool.
    pub fn is_connection_lost(&self) -> bool {
        match self {
            DriverError::Connection(_)
            | DriverError::PoolTimeout(_)
            | DriverError::PoolClosed(_)
            | DriverError::ConnectFailed(..) => true,
            DriverError::Server(server_error) => server_error.class == ServerErrorClass::Connection,
            _ => false,
        }
    }
}

impl From<PoolCheckoutError> for DriverError {
    fn from(error: PoolCheckoutError) -> Self {
        match error {
            PoolCheckoutError::Timeout(message) => DriverError::PoolTimeout(message),
            PoolCheckoutError::ConnectFailed(message, sqlstate) => DriverError::ConnectFailed(message, sqlstate),
            PoolCheckoutError::Closed(message) => DriverError::PoolClosed(message),
            PoolCheckoutError::Other(message) => DriverError::Connection(message),
        }
    }
}

impl From<tokio_postgres::Error> for DriverError {
    fn from(error: tokio_postgres::Error) -> Self {
        if let Some(database_error) = error.as_db_error() {
            return DriverError::Server(Box::new(ServerError::from_db_error(database_error)));
        }
        // No server error: a dead connection (the request channel closed, or the socket failed
        // mid-query as a wrapped io::Error), or a client-side failure.
        if error.is_closed() || error.source().is_some_and(|source| source.downcast_ref::<std::io::Error>().is_some()) {
            return DriverError::Connection(DriverError::message_with_causes(&error));
        }
        DriverError::Query(DriverError::message_with_causes(&error))
    }
}
