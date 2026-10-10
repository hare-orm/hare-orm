//! `URLField`: a printable ASCII URL `scheme://host[:port][/path, ?query or #fragment]` whose host
//! is a host name, `localhost` or an IPv4 address - the forms of `URLValidator` checked here. User
//! info (`user@`), an IPv6 host, a space or a non-ASCII character are left to the field.

use crate::codecs::host_name;

/// `URLValidator.MAX_URL_LENGTH`.
const MAX_URL_LENGTH: usize = 2048;
/// `URLValidator.MAX_HOSTNAME_LENGTH`.
const MAX_HOSTNAME_LENGTH: usize = 253;
/// The most digits of a port.
const MAX_PORT_DIGITS: usize = 5;

pub struct UrlFormat {
    /// `URLField.schemes` - lowercase.
    schemes: Vec<String>,
}

impl UrlFormat {
    pub fn new(schemes: Vec<String>) -> Self {
        UrlFormat { schemes }
    }

    /// Whether the URL is valid - false when the field decides.
    pub fn is_valid(&self, text: &str) -> bool {
        if text.len() > MAX_URL_LENGTH || !text.bytes().all(|byte| byte.is_ascii_graphic()) {
            return false;
        }
        let Some(scheme_end) = text.find(':') else {
            return false;
        };
        let scheme = &text[..scheme_end];
        let Some(after_scheme) = text[scheme_end..].strip_prefix("://") else {
            return false;
        };
        if !self.is_allowed_scheme(scheme) {
            return false;
        }
        let netloc = &after_scheme[..after_scheme.find(['/', '?', '#']).unwrap_or(after_scheme.len())];
        if netloc.contains(['@', '[', ']']) {
            return false;
        }
        let host = match netloc.split_once(':') {
            Some((host, port)) => {
                if port.is_empty() || port.len() > MAX_PORT_DIGITS || !port.bytes().all(|byte| byte.is_ascii_digit()) {
                    return false;
                }
                host
            }
            None => netloc,
        };
        !host.is_empty()
            && host.len() <= MAX_HOSTNAME_LENGTH
            && (is_ipv4_address(host) || host_name::is_host_name(host, true) || host.eq_ignore_ascii_case("localhost"))
    }

    fn is_allowed_scheme(&self, scheme: &str) -> bool {
        scheme.as_bytes().first().is_some_and(u8::is_ascii_alphabetic)
            && scheme.bytes().all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'+' | b'-' | b'.'))
            && self.schemes.iter().any(|allowed| allowed.eq_ignore_ascii_case(scheme))
    }
}

/// Whether `host` is four numbers from 0 to 255 joined by dots, written without leading zeros.
fn is_ipv4_address(host: &str) -> bool {
    let mut count = 0;
    for octet in host.split('.') {
        count += 1;
        let bytes = octet.as_bytes();
        if bytes.is_empty()
            || bytes.len() > 3
            || !bytes.iter().all(u8::is_ascii_digit)
            || (bytes.len() > 1 && bytes[0] == b'0')
            || octet.parse::<u16>().map_or(true, |number| number > 255)
        {
            return false;
        }
    }
    count == 4
}
