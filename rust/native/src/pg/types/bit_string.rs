//! `BIT`/`VARBIT`.

use crate::pg::value::{BoxError, Value};
use postgres_protocol::types::varbit_from_sql;

/// `BIT`/`VARBIT`: a 4-byte bit count, then the bits packed MSB-first - read as a string of
/// `'0'`/`'1'` characters, like asyncpg's `BitString.to01()`.
pub(crate) fn decode_bit_string(raw: &[u8]) -> Result<Value, BoxError> {
    let value = varbit_from_sql(raw)?;
    let mut text = String::with_capacity(value.len());
    for bit_index in 0..value.len() {
        let byte = value.bytes()[bit_index / 8];
        let bit = (byte >> (7 - (bit_index % 8))) & 1;
        text.push(if bit == 1 { '1' } else { '0' });
    }
    Ok(Value::Text(text))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bit_renders_as_a_string_of_zero_and_one_characters() {
        let mut raw = Vec::new();
        raw.extend_from_slice(&5i32.to_be_bytes());
        raw.push(0b1011_0000);
        let value = decode_bit_string(&raw).expect("well-formed bit payload must decode");
        assert!(matches!(value, Value::Text(text) if text == "10110"));
    }

    #[test]
    fn bit_rejects_negative_length_prefix() {
        let raw = (-1i32).to_be_bytes();
        assert!(decode_bit_string(&raw).is_err());
    }
}
