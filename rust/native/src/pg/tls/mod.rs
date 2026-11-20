//! PostgreSQL TLS configuration, adapted from yara-orm's MIT-licensed connector setup.
mod accept_any_cert;
mod chain_only;
mod enums;
mod ssl_params;

pub(crate) use enums::{CertCheck, PgTls};
pub(crate) use ssl_params::SslParams;
