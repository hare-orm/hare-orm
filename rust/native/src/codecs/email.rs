//! `EmailField`: an ASCII address of a dot-atom local part and a host name domain - the forms of
//! `EmailValidator` checked here. A quoted local part, an address literal (`[192.0.2.1]`) or a
//! non-ASCII character are left to the field.

use std::borrow::Cow;

use crate::codecs::host_name;

/// `EmailValidator.MAX_EMAIL_LENGTH`.
const MAX_EMAIL_LENGTH: usize = 320;

pub struct EmailFormat {
    /// `EmailField.lowercase` - the whole address is written lowercase, not only its domain.
    lowercase: bool,
}

impl EmailFormat {
    pub fn new(lowercase: bool) -> Self {
        EmailFormat { lowercase }
    }

    /// The address as `EmailField` writes it - the domain lowercase, all of it with `lowercase` -
    /// when it is valid; None when the field decides.
    pub fn normalize<'text>(&self, text: &'text str) -> Option<Cow<'text, str>> {
        if !text.is_ascii() || text.len() > MAX_EMAIL_LENGTH {
            return None;
        }
        let (local_part, domain) = text.rsplit_once('@')?;
        if !is_dot_atom(local_part) || !host_name::is_host_name(domain, false) {
            return None;
        }
        let lowercase_part = if self.lowercase { text } else { domain };
        if !lowercase_part.bytes().any(|byte| byte.is_ascii_uppercase()) {
            return Some(Cow::Borrowed(text));
        }
        Some(Cow::Owned(if self.lowercase {
            text.to_ascii_lowercase()
        } else {
            format!("{local_part}@{}", domain.to_ascii_lowercase())
        }))
    }
}

/// Whether `local_part` is atoms of letters, digits and ``-!#$%&'*+/=?^_`{}|~`` joined by dots.
fn is_dot_atom(local_part: &str) -> bool {
    local_part.split('.').all(|atom| !atom.is_empty() && atom.bytes().all(is_atom_byte))
}

fn is_atom_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || b"-!#$%&'*+/=?^_`{}|~".contains(&byte)
}
