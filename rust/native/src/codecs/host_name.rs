//! A host name as the host name pattern of `EmailValidator` and `URLValidator` accepts it,
//! matched case-insensitively, for ASCII text: labels split by dots - the first of 1 to
//! 63 letters, digits and hyphens starting and ending with a letter or digit, the middle ones of 1
//! to 63 letters, digits and hyphens not starting or ending with a hyphen, the last (the top-level
//! domain) of 2 to 63 letters and hyphens not starting or ending with a hyphen, or `xn--` and 1 to
//! 59 letters and digits.

/// The longest label.
const MAX_LABEL_LENGTH: usize = 63;
/// The longest top-level domain after `xn--`.
const MAX_PUNYCODE_LENGTH: usize = 59;

/// Whether `host` - ASCII text - is a host name, a trailing dot allowed with `allow_trailing_dot`.
pub fn is_host_name(host: &str, allow_trailing_dot: bool) -> bool {
    let host = if allow_trailing_dot { host.strip_suffix('.').unwrap_or(host) } else { host };
    let mut labels = host.split('.');
    let Some(first_label) = labels.next() else {
        return false;
    };
    if !is_first_label(first_label.as_bytes()) {
        return false;
    }
    let mut last_label = None;
    for label in labels {
        if let Some(middle_label) = last_label {
            if !is_middle_label(middle_label) {
                return false;
            }
        }
        last_label = Some(label.as_bytes());
    }
    last_label.is_some_and(is_top_level_label)
}

fn is_label_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'-'
}

fn is_first_label(label: &[u8]) -> bool {
    matches!((label.first(), label.last()), (Some(first), Some(last)) if first.is_ascii_alphanumeric() && last.is_ascii_alphanumeric())
        && label.len() <= MAX_LABEL_LENGTH
        && label.iter().all(|byte| is_label_byte(*byte))
}

fn is_middle_label(label: &[u8]) -> bool {
    matches!((label.first(), label.last()), (Some(first), Some(last)) if *first != b'-' && *last != b'-')
        && label.len() <= MAX_LABEL_LENGTH
        && label.iter().all(|byte| is_label_byte(*byte))
}

fn is_top_level_label(label: &[u8]) -> bool {
    let is_word = (2..=MAX_LABEL_LENGTH).contains(&label.len())
        && label.first() != Some(&b'-')
        && label.last() != Some(&b'-')
        && label.iter().all(|byte| byte.is_ascii_alphabetic() || *byte == b'-');
    let is_punycode = label.len() > 4
        && label[..4].eq_ignore_ascii_case(b"xn--")
        && label.len() - 4 <= MAX_PUNYCODE_LENGTH
        && label[4..].iter().all(u8::is_ascii_alphanumeric);
    is_word || is_punycode
}
