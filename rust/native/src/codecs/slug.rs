//! `SlugField`: ASCII letters, digits, hyphens and underscores. A non-ASCII character is left to the
//! field - with `allow_unicode` it decides which ones are letters and digits.

/// Whether `text` is a slug - false when the field decides.
pub fn is_slug(text: &str) -> bool {
    !text.is_empty() && text.bytes().all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}
