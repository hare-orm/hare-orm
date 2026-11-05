use super::accept_any_cert::AcceptAnyCert;
use super::chain_only::ChainOnly;
use super::CertCheck;
use crate::pg::error::DriverError;
use rustls::client::danger::ServerCertVerifier;
use rustls::client::WebPkiServerVerifier;
use rustls::RootCertStore;
use std::fs::File;
use std::io::BufReader;
use std::sync::{Arc, OnceLock};
use tokio_postgres_rustls::MakeRustlsConnect;

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub(crate) struct SslParams {
    pub(crate) check: CertCheck,
    pub(crate) root_cert: Option<String>,
}

static TLS_NO_VERIFY: OnceLock<MakeRustlsConnect> = OnceLock::new();

static TLS_VERIFY_CHAIN: OnceLock<MakeRustlsConnect> = OnceLock::new();

static TLS_VERIFY_FULL: OnceLock<MakeRustlsConnect> = OnceLock::new();

impl SslParams {
    pub(crate) fn connector(&self) -> Result<MakeRustlsConnect, DriverError> {
        if self.root_cert.is_some() {
            return self.build();
        }
        let cache = match self.check {
            CertCheck::None => &TLS_NO_VERIFY,
            CertCheck::Chain => &TLS_VERIFY_CHAIN,
            CertCheck::Full => &TLS_VERIFY_FULL,
        };
        if let Some(connector) = cache.get() {
            return Ok(connector.clone());
        }
        let connector = self.build()?;
        Ok(cache.get_or_init(|| connector).clone())
    }

    fn root_store(extra_ca: Option<&str>) -> Result<RootCertStore, DriverError> {
        let mut roots = RootCertStore::empty();
        for cert in rustls_native_certs::load_native_certs().certs {
            let _ = roots.add(cert);
        }
        let Some(path) = extra_ca else {
            return Ok(roots);
        };
        let file = File::open(path)
            .map_err(|error| DriverError::Config(format!("ssl_root_cert {path:?} cannot be read: {error}")))?;
        let mut added = 0usize;
        for cert in rustls_pemfile::certs(&mut BufReader::new(file)) {
            let cert =
                cert.map_err(|error| DriverError::Config(format!("ssl_root_cert {path:?} is not PEM: {error}")))?;
            roots
                .add(cert)
                .map_err(|error| DriverError::Config(format!("ssl_root_cert {path:?} is unusable: {error}")))?;
            added += 1;
        }
        if added == 0 {
            return Err(DriverError::Config(format!("ssl_root_cert {path:?} holds no certificates")));
        }
        Ok(roots)
    }

    fn build(&self) -> Result<MakeRustlsConnect, DriverError> {
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let builder = rustls::ClientConfig::builder_with_provider(provider.clone())
            .with_safe_default_protocol_versions()
            .map_err(|error| DriverError::Config(error.to_string()))?;
        let verifier: Arc<dyn ServerCertVerifier> = match self.check {
            CertCheck::None => Arc::new(AcceptAnyCert(provider)),
            check => {
                let roots = Self::root_store(self.root_cert.as_deref())?;
                let webpki = WebPkiServerVerifier::builder_with_provider(Arc::new(roots), provider)
                    .build()
                    .map_err(|error| DriverError::Config(error.to_string()))?;
                if check == CertCheck::Full {
                    webpki
                } else {
                    Arc::new(ChainOnly(webpki))
                }
            }
        };
        let config = builder.dangerous().with_custom_certificate_verifier(verifier).with_no_client_auth();
        Ok(MakeRustlsConnect::new(config))
    }
}
