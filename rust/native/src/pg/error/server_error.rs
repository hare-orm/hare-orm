use std::fmt::Write;
use tokio_postgres::error::ErrorPosition;

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

/// A server `ErrorResponse`: its class, the exception message (with asyncpg's `DETAIL:`/`HINT:`
/// lines) and its diagnostic fields, in `SERVER_ERROR_FIELD_NAMES` order.
#[derive(Debug)]
pub struct ServerError {
    pub class: ServerErrorClass,
    pub message: String,
    pub fields: [Option<String>; 17],
}

impl ServerError {
    pub(super) fn from_db_error(database_error: &tokio_postgres::error::DbError) -> Self {
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
