use tokio_postgres_rustls::MakeRustlsConnect;

/// How far the server's certificate is checked, per libpq's `sslmode` semantics (the semantics
/// callers coming from asyncpg/psycopg were written against): `prefer`/`require` encrypt without
/// authenticating; `verify-ca`/`verify-full` are the modes that actually check the certificate.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub(crate) enum CertCheck {
    #[default]
    None,
    Chain,
    Full,
}

/// The TLS settings of the pool, kept for the dedicated connection `Client::listen()` opens.
#[derive(Clone)]
pub(crate) enum PgTls {
    Disabled,
    Rustls(MakeRustlsConnect),
}
