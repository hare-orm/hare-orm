//! The driver's error type and the Python exceptions it raises. `DriverError` is adapted from
//! yara-orm's `EngineError` (<https://github.com/vsdudakov/yara-orm>, MIT License). The Python client
//! maps these exceptions to `hare.exceptions`, so this crate never imports hare's own modules.

use std::error::Error as StdError;
use std::fmt::Write;

use pyo3::create_exception;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use thiserror::Error;
use tokio_postgres::error::ErrorPosition;

create_exception!(rust.native.pg, ConnectionError, pyo3::exceptions::PyException);
create_exception!(rust.native.pg, QueryError, pyo3::exceptions::PyException);
create_exception!(rust.native.pg, IntegrityViolationError, QueryError);
create_exception!(rust.native.pg, ConversionError, pyo3::exceptions::PyException);
// SQLSTATE class 3D ("database does not exist"). Not a QueryError: dropping a missing database
// catches exactly this one.
create_exception!(rust.native.pg, InvalidCatalogError, pyo3::exceptions::PyException);
// A transaction method called after the transaction committed or rolled back - kept apart from an
// unrelated RuntimeError so a cleanup rollback can ignore exactly this one.
create_exception!(rust.native.pg, TransactionFinishedError, pyo3::exceptions::PyException);
// COMMIT/ROLLBACK on a connection already closed: the statement certainly never reached the server,
// unlike a ConnectionError while a COMMIT is in flight, whose outcome is unknown.
create_exception!(rust.native.pg, ConnectionClosedError, ConnectionError);
// SQLSTATE class 25 (e.g. 25P02, "current transaction is aborted") - the transaction, not the
// statement, is what's invalid.
create_exception!(rust.native.pg, InvalidTransactionStateError, pyo3::exceptions::PyException);

/// Which Python exception a server-reported error surfaces as.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ServerErrorClass {
    /// The server ended the session (SQLSTATE 57P01-57P04) - `ConnectionError`.
    Connection,
    /// Any other statement failure - `QueryError`.
    Query,
    /// SQLSTATE class 23 (unique, foreign key, not null, check, exclusion) - `IntegrityViolationError`.
    Integrity,
    /// SQLSTATE class 3D (invalid catalog name) - `InvalidCatalogError`.
    InvalidCatalog,
    /// SQLSTATE class 25 (invalid transaction state) - `InvalidTransactionStateError`.
    InvalidTransactionState,
}

/// The diagnostic attributes a server-reported error's exception carries, named like asyncpg's.
/// Every exception class defaults them to `None`.
const SERVER_ERROR_FIELD_NAMES: [&str; 17] = [
    "severity",
    "severity_en",
    "sqlstate",
    "message",
    "detail",
    "hint",
    "position",
    "internal_position",
    "internal_query",
    "context",
    "schema_name",
    "table_name",
    "column_name",
    "data_type_name",
    "constraint_name",
    "server_source_filename",
    "server_source_line",
];

/// A server `ErrorResponse`: its class, the exception message (with asyncpg's `DETAIL:`/`HINT:`
/// lines) and its diagnostic fields, in `SERVER_ERROR_FIELD_NAMES` order.
#[derive(Debug)]
pub struct ServerError {
    pub class: ServerErrorClass,
    pub message: String,
    pub fields: [Option<String>; 17],
}

impl ServerError {
    fn from_db_error(database_error: &tokio_postgres::error::DbError) -> Self {
        let code = database_error.code().code();
        let class = if code.starts_with("23") {
            ServerErrorClass::Integrity
        } else if code.starts_with("3D") {
            ServerErrorClass::InvalidCatalog
        } else if code.starts_with("25") {
            ServerErrorClass::InvalidTransactionState
        } else if matches!(code, "57P01" | "57P02" | "57P03" | "57P04") {
            // The server tore the session down; 57014 query_canceled leaves it usable.
            ServerErrorClass::Connection
        } else {
            ServerErrorClass::Query
        };
        let mut message = database_error.message().to_string();
        if class == ServerErrorClass::Query {
            let _ = write!(message, " (SQLSTATE {code})");
        }
        if let Some(detail) = database_error.detail() {
            let _ = write!(message, "\nDETAIL:  {detail}");
        }
        if let Some(hint) = database_error.hint() {
            let _ = write!(message, "\nHINT:  {hint}");
        }
        let (position, internal_position, internal_query) = match database_error.position() {
            Some(ErrorPosition::Original(position)) => (Some(position.to_string()), None, None),
            Some(ErrorPosition::Internal { position, query }) => {
                (None, Some(position.to_string()), Some(query.clone()))
            }
            None => (None, None, None),
        };
        let owned = |value: Option<&str>| value.map(str::to_string);
        let fields = [
            Some(database_error.severity().to_string()),
            database_error.parsed_severity().map(|severity| severity.to_string()),
            Some(code.to_string()),
            Some(database_error.message().to_string()),
            owned(database_error.detail()),
            owned(database_error.hint()),
            position,
            internal_position,
            internal_query,
            owned(database_error.where_()),
            owned(database_error.schema()),
            owned(database_error.table()),
            owned(database_error.column()),
            owned(database_error.datatype()),
            owned(database_error.constraint()),
            owned(database_error.file()),
            database_error.line().map(|line| line.to_string()),
        ];
        ServerError { class, message, fields }
    }
}

#[derive(Debug, Error)]
pub enum DriverError {
    #[error("configuration error: {0}")]
    Config(String),
    #[error("connection error: {0}")]
    Connection(String),
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
    pub fn from_pool_error(error: deadpool_postgres::PoolError) -> Self {
        match error {
            deadpool_postgres::PoolError::Backend(backend_error) if backend_error.as_db_error().is_some() => {
                DriverError::from(backend_error)
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
            DriverError::Connection(_) => true,
            DriverError::Server(server_error) => server_error.class == ServerErrorClass::Connection,
            _ => false,
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

pub fn to_pyerr(error: DriverError) -> PyErr {
    match error {
        DriverError::Config(message) => PyValueError::new_err(message),
        DriverError::Connection(message) => ConnectionError::new_err(message),
        DriverError::Query(message) => QueryError::new_err(message),
        DriverError::Conversion(message) => ConversionError::new_err(message),
        DriverError::Server(server_error) => {
            let ServerError { class, message, fields } = *server_error;
            let python_error = match class {
                ServerErrorClass::Connection => ConnectionError::new_err(message),
                ServerErrorClass::Query => QueryError::new_err(message),
                ServerErrorClass::Integrity => IntegrityViolationError::new_err(message),
                ServerErrorClass::InvalidCatalog => InvalidCatalogError::new_err(message),
                ServerErrorClass::InvalidTransactionState => InvalidTransactionStateError::new_err(message),
            };
            Python::attach(|py| {
                let value = python_error.value(py);
                for (name, field_value) in SERVER_ERROR_FIELD_NAMES.into_iter().zip(fields) {
                    // A plain attribute on a fresh exception instance cannot fail to set.
                    let _ = value.setattr(name, field_value);
                }
            });
            python_error
        }
    }
}

pub fn register(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("ConnectionError", py.get_type::<ConnectionError>())?;
    module.add("QueryError", py.get_type::<QueryError>())?;
    module.add("IntegrityViolationError", py.get_type::<IntegrityViolationError>())?;
    module.add("ConversionError", py.get_type::<ConversionError>())?;
    module.add("InvalidCatalogError", py.get_type::<InvalidCatalogError>())?;
    module.add("InvalidTransactionStateError", py.get_type::<InvalidTransactionStateError>())?;
    module.add("TransactionFinishedError", py.get_type::<TransactionFinishedError>())?;
    module.add("ConnectionClosedError", py.get_type::<ConnectionClosedError>())?;
    for exception_type in [
        py.get_type::<ConnectionError>(),
        py.get_type::<QueryError>(),
        py.get_type::<ConversionError>(),
        py.get_type::<InvalidCatalogError>(),
        py.get_type::<InvalidTransactionStateError>(),
    ] {
        for name in SERVER_ERROR_FIELD_NAMES {
            exception_type.setattr(name, py.None())?;
        }
    }
    Ok(())
}
