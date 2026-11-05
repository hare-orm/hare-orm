//! `BIT`/`VARBIT`.

use postgres_protocol::types::varbit_from_sql;
use tokio_postgres::types::{FromSql, Type};

use crate::pg::value::BoxError;

/// `BIT`/`VARBIT`: a 4-byte bit count, then the bits packed MSB-first - read as a string of
/// `'0'`/`'1'` characters, like asyncpg's `BitString.to01()`.
pub(crate) struct PgBitText(pub(crate) String);

impl<'a> FromSql<'a> for PgBitText {
    fn from_sql(_ty: &Type, raw: &'a [u8]) -> Result<Self, BoxError> {
        let value = varbit_from_sql(raw)?;
        let mut text = String::with_capacity(value.len());
        for bit_index in 0..value.len() {
            let byte = value.bytes()[bit_index / 8];
            let bit = (byte >> (7 - (bit_index % 8))) & 1;
            text.push(if bit == 1 { '1' } else { '0' });
        }
        Ok(PgBitText(text))
    }

    fn accepts(postgres_type: &Type) -> bool {
        *postgres_type == Type::BIT || *postgres_type == Type::VARBIT
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    use tokio_postgres::types::{FromSql, Type};

    #[test]
    fn bit_renders_as_a_string_of_zero_and_one_characters() {
        let mut raw = Vec::new();
        raw.extend_from_slice(&5i32.to_be_bytes());
        raw.push(0b1011_0000);
        let value = PgBitText::from_sql(&Type::BIT, &raw).expect("well-formed bit payload must decode");
        assert_eq!(value.0, "10110");
    }

    #[test]
    fn bit_rejects_negative_length_prefix() {
        let raw = (-1i32).to_be_bytes();
        assert!(PgBitText::from_sql(&Type::BIT, &raw).is_err());
    }
}
