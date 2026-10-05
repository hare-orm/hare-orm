//! `PhoneField` without `phonenumbers`: a number in E.164 form - `+`, a digit from 1 to 9 and 1 to
//! 14 more digits.

/// The most digits of an E.164 number.
const MAX_DIGITS: usize = 15;

/// Whether `text` is an E.164 number - false when the field decides.
pub fn is_e164_number(text: &str) -> bool {
    let Some(digits) = text.strip_prefix('+') else {
        return false;
    };
    let bytes = digits.as_bytes();
    (2..=MAX_DIGITS).contains(&bytes.len()) && matches!(bytes[0], b'1'..=b'9') && bytes.iter().all(u8::is_ascii_digit)
}
