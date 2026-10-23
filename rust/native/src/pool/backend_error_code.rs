//! `BackendErrorCode` - the code a database server gave the error of a native driver's backend (a
//! SQLSTATE for Postgres), so a failed checkout keeps why the server refused the connection.

pub(crate) trait BackendErrorCode {
    /// The server's code for the error, None for an error the server didn't report.
    fn get_code(&self) -> Option<String>;
}
