//! A column's decoding strategy selected from its physical PostgreSQL type, never sample bytes.

use std::fmt::Write as _;

use tokio_postgres::types::{Field, FromSql, Kind as TypeShape, Type};

use crate::pg::decode_context::PgDecodeContext;
use crate::pg::types::array::decode_array;
use crate::pg::types::bit_string::decode_bit_string;
use crate::pg::types::composite::decode_composite;
use crate::pg::types::datetime::{decode_date, decode_timestamp, time_of_day};
use crate::pg::types::interval::decode_interval;
use crate::pg::types::json::decode_json_text;
use crate::pg::types::mac_address::decode_macaddr;
use crate::pg::types::money::decode_money;
use crate::pg::types::network::decode_network;
use crate::pg::types::numeric::decode_numeric;
use crate::pg::types::range::{decode_multirange, decode_range};
use crate::pg::types::text_search_query::PgTsQueryText;
use crate::pg::types::text_search_vector::PgTsVectorText;
use crate::pg::types::time_with_zone::decode_time_with_zone;
use crate::pg::types::vector::PgVectorCell;
use crate::pg::value::decode::{ServerTextForms, RECORD_OID};
use crate::pg::value::{domain_base_type, BoxError, Value};

type ScalarDecoder = fn(&Type, &[u8], &mut PgDecodeContext) -> Result<Value, BoxError>;

enum ColumnDecoding<'a> {
    Scalar(&'a Type, ScalarDecoder),
    Array(&'a Type),
    RecordArray,
    Composite(Option<&'a [Field]>),
    Range(&'a Type),
    Multirange(&'a Type),
    ServerText(&'a Type),
}

pub(crate) struct PgColumnDecoder<'a>(ColumnDecoding<'a>);

impl<'a> PgColumnDecoder<'a> {
    pub(crate) fn new(postgres_type: &'a Type) -> Self {
        let postgres_type = domain_base_type(postgres_type);
        let decoding = if let Some(decoder) = Self::scalar_decoder(postgres_type) {
            ColumnDecoding::Scalar(postgres_type, decoder)
        } else if postgres_type.oid() == RECORD_OID {
            ColumnDecoding::Composite(None)
        } else if *postgres_type == Type::RECORD_ARRAY {
            ColumnDecoding::RecordArray
        } else {
            match postgres_type.kind() {
                TypeShape::Array(member) => ColumnDecoding::Array(member),
                TypeShape::Enum(_) => ColumnDecoding::Scalar(postgres_type, Self::decode_text),
                TypeShape::Range(_) => ColumnDecoding::Range(postgres_type),
                TypeShape::Multirange(subtype) => ColumnDecoding::Multirange(subtype),
                TypeShape::Composite(fields) => ColumnDecoding::Composite(Some(fields)),
                _ => match postgres_type.name() {
                    "vector" => ColumnDecoding::Scalar(postgres_type, Self::decode_vector),
                    "citext" => ColumnDecoding::Scalar(postgres_type, Self::decode_text),
                    "geometry" | "geography" => {
                        ColumnDecoding::Scalar(postgres_type, |_, raw, _| Ok(Self::decode_spatial_hex(raw)))
                    }
                    _ => ColumnDecoding::ServerText(postgres_type),
                },
            }
        };
        Self(decoding)
    }

    pub(crate) fn decode<'wire>(
        &self,
        raw: Option<&'wire [u8]>,
        forms: &mut ServerTextForms<'wire>,
    ) -> Result<Value, BoxError> {
        match raw {
            Some(raw) => self.decode_with_context(raw, forms, &mut PgDecodeContext::new(raw.len())),
            None => Ok(Value::Null),
        }
    }

    pub(crate) fn decode_with_context<'wire>(
        &self,
        raw: &'wire [u8],
        forms: &mut ServerTextForms<'wire>,
        context: &mut PgDecodeContext,
    ) -> Result<Value, BoxError> {
        context.visit(1, |context| {
            let value = match self.0 {
                ColumnDecoding::Scalar(postgres_type, decode) => decode(postgres_type, raw, context),
                ColumnDecoding::Array(member) => decode_array(member, raw, forms, context),
                ColumnDecoding::RecordArray => decode_array(&Type::RECORD, raw, forms, context),
                ColumnDecoding::Composite(fields) => decode_composite(raw, fields, forms, context),
                ColumnDecoding::Range(postgres_type) => {
                    decode_range(postgres_type, raw, forms, context).map(Value::Range)
                }
                ColumnDecoding::Multirange(subtype) => decode_multirange(subtype, raw, forms, context),
                ColumnDecoding::ServerText(postgres_type) => Ok(forms.text_form(postgres_type, raw)),
            }?;
            match &value {
                Value::Text(text) | Value::Json(text) | Value::BigInt(text) | Value::Decimal(text) => {
                    context.reserve_bytes(text.len())?;
                }
                Value::Bytes(bytes) => context.reserve_bytes(bytes.len())?,
                _ => {}
            }
            Ok(value)
        })
    }

    fn decode_text(_: &Type, raw: &[u8], _: &mut PgDecodeContext) -> Result<Value, BoxError> {
        Ok(Value::Text(std::str::from_utf8(raw)?.to_string()))
    }

    fn decode_vector(postgres_type: &Type, raw: &[u8], context: &mut PgDecodeContext) -> Result<Value, BoxError> {
        let cell = PgVectorCell::from_sql(postgres_type, raw)?;
        context.reserve_values(cell.0.len())?;
        Ok(cell.into_value())
    }

    fn decode_spatial_hex(raw: &[u8]) -> Value {
        // PostGIS's binary form is EWKB; its text form is the same bytes in uppercase hex.
        let mut hex = String::with_capacity(raw.len() * 2);
        for byte in raw {
            write!(hex, "{byte:02X}").expect("writing hex digits into a String never fails");
        }
        Value::Text(hex)
    }

    /// Returning a decoder declares support even when the actual payload is invalid or empty.
    fn scalar_decoder(postgres_type: &Type) -> Option<ScalarDecoder> {
        let decoder: ScalarDecoder = match postgres_type.oid() {
            25 | 1043 | 1042 | 19 | 142 | 2275 | 1790 | 705 => Self::decode_text,
            16 => |ty, raw, _| bool::from_sql(ty, raw).map(Value::Bool),
            21 => |ty, raw, _| i16::from_sql(ty, raw).map(|value| Value::Int(value.into())),
            23 => |ty, raw, _| i32::from_sql(ty, raw).map(|value| Value::Int(value.into())),
            20 => |ty, raw, _| i64::from_sql(ty, raw).map(Value::Int),
            700 => |ty, raw, _| f32::from_sql(ty, raw).map(|value| Value::Float(value.into())),
            701 => |ty, raw, _| f64::from_sql(ty, raw).map(Value::Float),
            114 | 3802 => |ty, raw, _| decode_json_text(ty, raw).map(|text| Value::Json(text.to_string())),
            2950 => |_, raw, _| uuid::Uuid::from_slice(raw).map(Value::Uuid).map_err(BoxError::from),
            1114 => |_, raw, _| decode_timestamp(raw).map(Value::Timestamp),
            1184 => |_, raw, _| decode_timestamp(raw).map(|value| Value::TimestampTz(value.and_utc())),
            1082 => |_, raw, _| decode_date(raw).map(Value::Date),
            17 | 18 => |_, raw, _| Ok(Value::Bytes(raw.to_vec())),
            1700 => |_, raw, _| decode_numeric(raw),
            1083 => |_, raw, _| i64::from_sql(&Type::INT8, raw).and_then(time_of_day).map(Value::Time),
            1266 => |_, raw, _| decode_time_with_zone(raw),
            790 => |_, raw, _| decode_money(raw),
            1186 => |_, raw, _| decode_interval(raw).map(Value::Interval),
            650 | 869 => |ty, raw, _| decode_network(ty, raw).map(Value::Network),
            829 | 774 => |_, raw, _| decode_macaddr(raw),
            1560 | 1562 => |_, raw, _| decode_bit_string(raw),
            3614 => |_, raw, context| PgTsVectorText::decode(raw, context).map(|value| Value::Text(value.0)),
            3615 => |_, raw, context| PgTsQueryText::decode(raw, context).map(|value| Value::Text(value.0)),
            26 | 28 | 29 => |ty, raw, _| {
                let bytes: [u8; 4] =
                    raw.try_into().map_err(|_| format!("invalid {} payload length {}", ty.name(), raw.len()))?;
                Ok(Value::Int(i64::from(u32::from_be_bytes(bytes))))
            },
            5069 => |_, raw, _| {
                let bytes: [u8; 8] =
                    raw.try_into().map_err(|_| format!("invalid xid8 payload length {}", raw.len()))?;
                let value = u64::from_be_bytes(bytes);
                Ok(i64::try_from(value).map_or_else(|_| Value::BigInt(value.to_string()), Value::Int))
            },
            2278 => |_, _, _| Ok(Value::Null),
            _ => return None,
        };
        Some(decoder)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn malformed_supported_values_do_not_fall_back_to_server_text() {
        for postgres_type in [Type::INT4, Type::NUMERIC, Type::UUID, Type::JSONB, Type::INT4_ARRAY] {
            let decoder = PgColumnDecoder::new(&postgres_type);
            let mut forms = ServerTextForms::collecting();
            assert!(decoder.decode(Some(&[]), &mut forms).is_err(), "{postgres_type}");
            assert_eq!(forms.take_requests(), [] as [(Type, &[u8]); 0]);
            assert!(matches!(decoder.decode(None, &mut forms).unwrap(), Value::Null));
        }
    }

    #[test]
    fn unknown_null_and_non_null_values_remain_distinct() {
        let postgres_type = Type::POINT;
        let decoder = PgColumnDecoder::new(&postgres_type);
        let mut forms = ServerTextForms::collecting();
        assert!(matches!(decoder.decode(None, &mut forms).unwrap(), Value::Null));
        assert_eq!(forms.take_requests(), [] as [(Type, &[u8]); 0]);
        assert!(matches!(decoder.decode(Some(&[]), &mut forms).unwrap(), Value::DeferredText(0)));
        assert_eq!(forms.take_requests(), vec![(Type::POINT, &[][..])]);
    }

    #[test]
    fn domains_preserve_the_base_json_wire_semantics() {
        let domain = Type::new("document".to_string(), 50_000, TypeShape::Domain(Type::JSONB), "public".to_string());
        let decoder = PgColumnDecoder::new(&domain);
        let mut forms = ServerTextForms::collecting();
        let value = decoder.decode(Some(b"\x01{\"value\":null}"), &mut forms).unwrap();
        assert!(matches!(value, Value::Json(text) if text == "{\"value\":null}"));
        assert_eq!(forms.take_requests(), [] as [(Type, &[u8]); 0]);
    }
}
