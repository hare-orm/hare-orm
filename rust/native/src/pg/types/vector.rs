//! pgvector's `vector`.

use std::fmt::Write as _;

use bytes::Buf;
use tokio_postgres::types::{FromSql, Type};

use crate::pg::value::{BoxError, Value};

/// pgvector's `vector` type (no fixed OID - matched by name): a big-endian `uint16` dimension
/// count, a reserved `uint16`, then that many big-endian `float4`s.
pub(crate) struct PgVectorCell(pub(crate) Vec<f32>);

impl PgVectorCell {
    /// The vector as a list of floats, each the shortest decimal that round-trips its `float4`
    /// (`0.1`, not `0.10000000149011612`) - the text `vector_out` prints, as asyncpg returns it.
    pub(crate) fn into_value(self) -> Value {
        let mut text = String::new();
        Value::Array(
            self.0
                .into_iter()
                .map(|element| {
                    text.clear();
                    write!(text, "{element}").expect("writing a float into a String never fails");
                    Value::Float(text.parse::<f64>().unwrap_or(f64::from(element)))
                })
                .collect(),
        )
    }
}

impl<'a> FromSql<'a> for PgVectorCell {
    fn from_sql(_ty: &Type, mut raw: &'a [u8]) -> Result<Self, BoxError> {
        if raw.len() < 4 {
            return Err(format!("invalid vector payload length {}", raw.len()).into());
        }
        let dimensions = raw.get_u16() as usize;
        let _reserved = raw.get_u16();
        let expected_len = dimensions * 4;
        if raw.len() != expected_len {
            return Err(format!(
                "vector payload has {} remaining byte(s), expected {} for {} dimension(s)",
                raw.len(),
                expected_len,
                dimensions
            )
            .into());
        }
        let mut values = Vec::with_capacity(dimensions);
        for _ in 0..dimensions {
            values.push(raw.get_f32());
        }
        Ok(PgVectorCell(values))
    }

    fn accepts(ty: &Type) -> bool {
        ty.name() == "vector"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    use tokio_postgres::types::{FromSql, Type};

    use crate::pg::value::test_support::*;
    use crate::pg::value::Value;

    #[test]
    fn vector_elements_decode_as_the_shortest_decimal_of_their_float4() {
        match PgVectorCell(vec![0.1, 0.2, 1.5, -3.25]).into_value() {
            Value::Array(items) => {
                let floats: Vec<f64> = items
                    .into_iter()
                    .map(|item| match item {
                        Value::Float(value) => value,
                        other => panic!("unexpected {other:?}"),
                    })
                    .collect();
                assert_eq!(floats, vec![0.1, 0.2, 1.5, -3.25]);
            }
            other => panic!("unexpected {other:?}"),
        }
    }
    /// The exact regression case: every byte of `2.0f32`/`0.0f32`'s big-endian representation
    /// happens to be `< 0x80`, so the whole wire payload passes as valid UTF-8 by accident - the
    /// condition a guess-by-UTF-8 decoder would misread as text.
    #[test]
    fn vector_decodes_a_round_valued_vector_whose_bytes_are_all_ascii_range() {
        let mut raw = Vec::new();
        raw.extend_from_slice(&3u16.to_be_bytes());
        raw.extend_from_slice(&0u16.to_be_bytes());
        for v in [2.0f32, 0.0f32, 0.0f32] {
            raw.extend_from_slice(&v.to_be_bytes());
        }
        let value = PgVectorCell::from_sql(&vector_type(), &raw).expect("well-formed vector payload must decode");
        assert_eq!(value.0, vec![2.0f32, 0.0, 0.0]);
    }

    #[test]
    fn vector_decodes_a_vector_whose_bytes_are_not_valid_utf8() {
        let mut raw = Vec::new();
        raw.extend_from_slice(&3u16.to_be_bytes());
        raw.extend_from_slice(&0u16.to_be_bytes());
        for v in [0.1f32, -0.2f32, 0.3f32] {
            raw.extend_from_slice(&v.to_be_bytes());
        }
        let value = PgVectorCell::from_sql(&vector_type(), &raw).expect("well-formed vector payload must decode");
        assert_eq!(value.0, vec![0.1f32, -0.2, 0.3]);
    }

    #[test]
    fn vector_decodes_an_empty_zero_dimension_vector() {
        let mut raw = Vec::new();
        raw.extend_from_slice(&0u16.to_be_bytes());
        raw.extend_from_slice(&0u16.to_be_bytes());
        let value = PgVectorCell::from_sql(&vector_type(), &raw).expect("zero-dimension vector payload must decode");
        assert_eq!(value.0, Vec::<f32>::new());
    }

    #[test]
    fn vector_rejects_a_payload_shorter_than_its_declared_dimension_count() {
        let mut raw = Vec::new();
        raw.extend_from_slice(&3u16.to_be_bytes());
        raw.extend_from_slice(&0u16.to_be_bytes());
        raw.extend_from_slice(&1.0f32.to_be_bytes()); // only 1 of 3 declared floats present
        assert!(PgVectorCell::from_sql(&vector_type(), &raw).is_err());
    }

    #[test]
    fn vector_accepts_by_type_name_since_its_oid_is_not_a_fixed_catalog_value() {
        assert!(PgVectorCell::accepts(&vector_type()));
        assert!(!PgVectorCell::accepts(&Type::TEXT));
    }
}
