//! The driver's error type and the Python exceptions it raises. `DriverError` is adapted from
//! yara-orm's `EngineError` (<https://github.com/vsdudakov/yara-orm>, MIT License). The Python client
//! maps these exceptions to `hare.exceptions`, so this crate never imports hare's own modules.

use crate::pool::backend_error_code::BackendErrorCode;
use pyo3::create_exception;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

mod driver_error;
mod server_error;

pub use driver_error::DriverError;
pub use server_error::ServerError;
pub use server_error::ServerErrorClass;

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

// Waiting for a pool connection ran out of the pool's wait timeout - the pool is busy, not broken.
create_exception!(rust.native.pg, PoolTimeoutError, ConnectionError);

// The pool grew and opening its new connection failed.
create_exception!(rust.native.pg, ConnectFailedError, ConnectionError);

// The pool was closed before a connection was taken from it - no statement reached the server.
create_exception!(rust.native.pg, PoolClosedError, ConnectionError);

// SQLSTATE class 25 (e.g. 25P02, "current transaction is aborted") - the transaction, not the
// statement, is what's invalid.
create_exception!(rust.native.pg, InvalidTransactionStateError, pyo3::exceptions::PyException);

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

impl BackendErrorCode for tokio_postgres::Error {
    fn get_code(&self) -> Option<String> {
        self.as_db_error().map(|database_error| database_error.code().code().to_string())
    }
}

pub fn to_pyerr(error: impl Into<DriverError>) -> PyErr {
    match error.into() {
        DriverError::Config(message) => PyValueError::new_err(message),
        DriverError::Connection(message) => ConnectionError::new_err(message),
        DriverError::PoolTimeout(message) => PoolTimeoutError::new_err(message),
        DriverError::PoolClosed(message) => PoolClosedError::new_err(message),
        DriverError::ConnectFailed(message, sqlstate) => {
            let python_error = ConnectFailedError::new_err(message);
            if sqlstate.is_some() {
                // A plain attribute on a fresh exception instance cannot fail to set.
                Python::attach(|py| {
                    let _ = python_error.value(py).setattr("sqlstate", sqlstate);
                });
            }
            python_error
        }
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
    module.add("PoolTimeoutError", py.get_type::<PoolTimeoutError>())?;
    module.add("ConnectFailedError", py.get_type::<ConnectFailedError>())?;
    module.add("PoolClosedError", py.get_type::<PoolClosedError>())?;
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
