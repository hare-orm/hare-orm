//! A `UUIDField`: read as a plain `uuid.UUID`, written as its canonical text.

mod uuid_read;
mod uuid_write;

pub use uuid_read::UuidRead;
pub use uuid_write::UuidWrite;

/// The value of canonical UUID text - 32 hex digits, or 36 with hyphens at 8, 13, 18 and 23.
pub fn parse_uuid(text: &str) -> Option<u128> {
    let bytes = text.as_bytes();
    let mut value: u128 = 0;
    let mut digit_count = 0;
    match bytes.len() {
        32 => {}
        36 => {
            if [8, 13, 18, 23].iter().any(|&index| bytes[index] != b'-') {
                return None;
            }
        }
        _ => return None,
    }
    for (index, &byte) in bytes.iter().enumerate() {
        if bytes.len() == 36 && matches!(index, 8 | 13 | 18 | 23) {
            continue;
        }
        let digit = char::from(byte).to_digit(16)?;
        value = (value << 4) | u128::from(digit);
        digit_count += 1;
    }
    (digit_count == 32).then_some(value)
}

/// `str(uuid)` of a 128-bit value - lowercase, hyphenated.
pub fn format_uuid(value: u128) -> String {
    let hex = format!("{value:032x}");
    format!("{}-{}-{}-{}-{}", &hex[0..8], &hex[8..12], &hex[12..16], &hex[16..20], &hex[20..32])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_canonical_text() {
        let value = 0x1234_5678_9abc_def0_1234_5678_9abc_def0_u128;
        assert_eq!(parse_uuid("12345678-9abc-def0-1234-56789abcdef0"), Some(value));
        assert_eq!(parse_uuid("123456789ABCDEF0123456789ABCDEF0"), Some(value));
        assert_eq!(format_uuid(value), "12345678-9abc-def0-1234-56789abcdef0");
    }

    #[test]
    fn rejects_other_text() {
        assert_eq!(parse_uuid("{12345678-9abc-def0-1234-56789abcdef0}"), None);
        assert_eq!(parse_uuid("12345678-9abc-def0-1234-56789abcdefg"), None);
        assert_eq!(parse_uuid("123456789-abc-def0-1234-56789abcdef0"), None);
        assert_eq!(parse_uuid("+2345678-9abc-def0-1234-56789abcdef0"), None);
        assert_eq!(parse_uuid(""), None);
    }
}
