//! One decision selects both the parameter's wire format and its encoder.
use crate::pg::types::array::{is_text_format_array, write_text_array_literal};
use crate::pg::value::{domain_base_type, BoxError, Value};
use bytes::BytesMut;
use tokio_postgres::types::{Format, IsNull, Kind as TypeShape, Type};

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum PgParameterMode {
    Negotiated,
    BinaryRequired,
}

#[derive(Clone, Copy)]
enum ParameterEncoder<'a> {
    Binary,
    Text(&'a str),
    TextArray(&'a [Value]),
}

pub(crate) struct PgParameterEncoding<'a> {
    value: &'a Value,
    postgres_type: &'a Type,
    encoder: ParameterEncoder<'a>,
}

impl<'a> PgParameterEncoding<'a> {
    pub(crate) fn new(value: &'a Value, postgres_type: &'a Type, mode: PgParameterMode) -> Self {
        let postgres_type = domain_base_type(postgres_type);
        let encoder = match (mode, value) {
            (PgParameterMode::Negotiated, Value::Text(text))
                if !matches!(*postgres_type, Type::UUID | Type::JSON | Type::JSONB) =>
            {
                ParameterEncoder::Text(text)
            }
            (PgParameterMode::Negotiated, Value::Array(items)) if is_text_format_array(value, postgres_type) => {
                ParameterEncoder::TextArray(items)
            }
            _ => ParameterEncoder::Binary,
        };
        Self { value, postgres_type, encoder }
    }

    pub(crate) fn format(&self) -> Format {
        match self.encoder {
            ParameterEncoder::Binary => Format::Binary,
            _ => Format::Text,
        }
    }

    pub(crate) fn encode(&self, out: &mut BytesMut) -> Result<IsNull, BoxError> {
        match self.encoder {
            ParameterEncoder::Binary => self.value.encode_binary(self.postgres_type, out),
            ParameterEncoder::Text(text) => {
                out.extend_from_slice(text.as_bytes());
                Ok(IsNull::No)
            }
            ParameterEncoder::TextArray(items) => {
                let mut literal = String::new();
                write_text_array_literal(items, &mut literal);
                out.extend_from_slice(literal.as_bytes());
                Ok(IsNull::No)
            }
        }
    }

    /// Types for which a string has an explicit binary conversion.
    pub(crate) fn accepts_text_in_binary(postgres_type: &Type) -> bool {
        let postgres_type = domain_base_type(postgres_type);
        matches!(
            *postgres_type,
            Type::TEXT
                | Type::VARCHAR
                | Type::BPCHAR
                | Type::NAME
                | Type::UNKNOWN
                | Type::JSON
                | Type::JSONB
                | Type::UUID
        ) || matches!(postgres_type.kind(), TypeShape::Enum(_))
            || postgres_type.name() == "citext"
    }

    /// Suggested bind type; None preserves server inference and overload selection.
    pub(crate) fn inferred_type(value: &Value) -> Option<Type> {
        Some(match value {
            Value::Bool(_) => Type::BOOL,
            Value::Bytes(_) => Type::BYTEA,
            Value::Uuid(_) => Type::UUID,
            Value::Decimal(_) | Value::NonFiniteDecimal(_) => Type::NUMERIC,
            Value::Float(_) => Type::FLOAT8,
            Value::TimestampTz(_) => Type::TIMESTAMPTZ,
            Value::Date(_) => Type::DATE,
            Value::Time(_) => Type::TIME,
            Value::TimeTz(..) => Type::TIMETZ,
            Value::Interval(_) => Type::INTERVAL,
            _ => return None,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn format_and_payload_follow_the_same_negotiation() {
        let value = Value::Text("123".to_string());
        let decision = PgParameterEncoding::new(&value, &Type::INT4, PgParameterMode::Negotiated);
        assert!(matches!(decision.format(), Format::Text));
        let mut out = BytesMut::new();
        decision.encode(&mut out).unwrap();
        assert_eq!(&out[..], b"123");
        let decision = PgParameterEncoding::new(&value, &Type::INT4, PgParameterMode::BinaryRequired);
        assert!(matches!(decision.format(), Format::Binary));
        let mut out = BytesMut::new();
        assert!(decision.encode(&mut out).is_err());
        assert!(out.is_empty());
    }

    #[test]
    fn declared_domains_use_the_base_wire_contract() {
        let domain = Type::new("positive".to_string(), 50_000, TypeShape::Domain(Type::INT4), "public".to_string());
        let value = Value::Int(12);
        let decision = PgParameterEncoding::new(&value, &domain, PgParameterMode::BinaryRequired);
        let mut out = BytesMut::new();
        assert!(matches!(decision.format(), Format::Binary));
        decision.encode(&mut out).unwrap();
        assert_eq!(&out[..], &12_i32.to_be_bytes());
        assert!(PgParameterEncoding::inferred_type(&value).is_none());
    }

    #[test]
    fn json_and_sql_arrays_select_distinct_encoders() {
        let value = Value::Array(vec![Value::Int(1), Value::Int(2)]);
        let mut json = BytesMut::new();
        PgParameterEncoding::new(&value, &Type::JSONB, PgParameterMode::BinaryRequired).encode(&mut json).unwrap();
        assert_eq!(&json[..], b"\x01[1,2]");
        let mut array = BytesMut::new();
        let decision = PgParameterEncoding::new(&value, &Type::INT4_ARRAY, PgParameterMode::Negotiated);
        assert!(matches!(decision.format(), Format::Binary));
        decision.encode(&mut array).unwrap();
        let expected: Vec<u8> = [1_i32, 0, 23, 2, 1, 4, 1, 4, 2].into_iter().flat_map(i32::to_be_bytes).collect();
        assert_eq!(&array[..], &expected);
    }
}
