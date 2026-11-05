//! A column's decoding strategy selected from its physical PostgreSQL type, never sample bytes.

use tokio_postgres::types::{FromSql, Kind as TypeShape, Type};

use crate::pg::decode_context::PgDecodeContext;
use crate::pg::types::bit_string::PgBitText;
use crate::pg::types::datetime::{decode_date, decode_timestamp, time_of_day};
use crate::pg::types::interval::decode_interval;
use crate::pg::types::json::decode_json_text;
use crate::pg::types::mac_address::decode_macaddr;
use crate::pg::types::money::decode_money;
use crate::pg::types::network::decode_network;
use crate::pg::types::numeric::decode_numeric;
use crate::pg::types::text_search_query::PgTsQueryText;
use crate::pg::types::text_search_vector::PgTsVectorText;
use crate::pg::types::time_with_zone::PgTimeTzCell;
use crate::pg::value::decode::{decode_structured_value, ServerTextForms, RECORD_OID};
use crate::pg::value::{domain_base_type, BoxError, Value};

type ScalarDecoder = fn(&Type, &[u8], &mut PgDecodeContext) -> Result<Value, BoxError>;

enum ColumnDecoding {
    Scalar(ScalarDecoder),
    Structured,
    ServerText,
}

pub(crate) struct PgColumnDecoder<'a> {
    postgres_type: &'a Type,
    decoding: ColumnDecoding,
}

impl<'a> PgColumnDecoder<'a> {
    pub(crate) fn new(postgres_type: &'a Type) -> Self {
        let postgres_type = domain_base_type(postgres_type);
        let decoding = if let Some(decoder) = Self::scalar_decoder(postgres_type) {
            ColumnDecoding::Scalar(decoder)
        } else if postgres_type.oid() == RECORD_OID
            || matches!(
                postgres_type.kind(),
                TypeShape::Array(_)
                    | TypeShape::Enum(_)
                    | TypeShape::Range(_)
                    | TypeShape::Multirange(_)
                    | TypeShape::Composite(_)
            )
            || matches!(postgres_type.name(), "vector" | "citext" | "geometry" | "geography")
        {
            ColumnDecoding::Structured
        } else {
            ColumnDecoding::ServerText
        };
        Self { postgres_type, decoding }
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
            let value = match self.decoding {
                ColumnDecoding::Scalar(decode) => decode(self.postgres_type, raw, context)?,
                ColumnDecoding::Structured => decode_structured_value(self.postgres_type, raw, forms, context)?,
                ColumnDecoding::ServerText => forms.text_form(self.postgres_type, raw),
            };
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

    /// Returning a decoder declares support even when the actual payload is invalid or empty.
    fn scalar_decoder(postgres_type: &Type) -> Option<ScalarDecoder> {
        let decoder: ScalarDecoder = match postgres_type.oid() {
            25 | 1043 | 1042 | 19 | 142 | 2275 | 1790 | 705 => {
                |_, raw, _| Ok(Value::Text(std::str::from_utf8(raw)?.to_string()))
            }
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
            1266 => |ty, raw, _| PgTimeTzCell::from_sql(ty, raw).map(|cell| Value::TimeTz(cell.0, cell.1)),
            790 => |_, raw, _| decode_money(raw),
            1186 => |_, raw, _| decode_interval(raw).map(Value::Interval),
            650 | 869 => |ty, raw, _| decode_network(ty, raw).map(Value::Network),
            829 | 774 => |_, raw, _| decode_macaddr(raw),
            1560 | 1562 => |ty, raw, _| PgBitText::from_sql(ty, raw).map(|value| Value::Text(value.0)),
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
