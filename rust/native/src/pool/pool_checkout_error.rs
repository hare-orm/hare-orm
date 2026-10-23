//! `PoolCheckoutError` - why a native pool gave no connection; each driver turns it into its own
//! error.

pub(crate) enum PoolCheckoutError {
    /// The wait for a connection ran out of the pool's wait timeout.
    Timeout(String),
    /// Opening a new connection failed - with the server's code for the refusal when the server
    /// refused it (`BackendErrorCode`).
    ConnectFailed(String, Option<String>),
    /// The pool was closed - no connection was taken, no statement sent.
    Closed(String),
    /// Anything else - a failing hook.
    Other(String),
}
