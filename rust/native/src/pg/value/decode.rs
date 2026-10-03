//! Wire bytes of a result column -> `Value`.

use std::fmt::Write as _;

use tokio_postgres::types::{FromSql, Kind as TypeShape, Type};

use crate::pg::error::DriverError;
use crate::pg::types::array::decode_array;
use crate::pg::types::bit_string::PgBitText;
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
use crate::pg::types::time_with_zone::PgTimeTzCell;
use crate::pg::types::vector::PgVectorCell;
use crate::pg::value::{BoxError, Value};

/// The anonymous `record` pseudo-type (`ROW(...)`).
pub(crate) const RECORD_OID: u32 = 2249;
/// How many values one server-side text-conversion query carries (see `ServerTextForms`).
pub(crate) const SERVER_TEXT_FORMS_PER_QUERY: usize = 10_000;

/// A column's raw wire bytes (`None` for SQL NULL) - `Row` exposes them only through `FromSql`.
pub(crate) struct RawColumnBytes<'a>(Option<&'a [u8]>);

impl<'a> FromSql<'a> for RawColumnBytes<'a> {
    fn from_sql(_ty: &Type, raw: &'a [u8]) -> Result<Self, BoxError> {
        Ok(RawColumnBytes(Some(raw)))
    }

    fn from_sql_null(_ty: &Type) -> Result<Self, BoxError> {
        Ok(RawColumnBytes(None))
    }

    fn accepts(_ty: &Type) -> bool {
        true
    }
}

/// Values of types this module has no binary decoder for, converted to their text form by the
/// server itself. tokio-postgres always requests binary results, so the first decoding pass
/// (`Collect`) records every such value's type and raw bytes; `fetch_server_text_forms` sends
/// them back as parameters of that same type cast to text (the type's own output function, what
/// a text-format result would have carried); the second pass (`Supply`) hands the texts out in
/// the same order.
pub(crate) enum ServerTextForms<'a> {
    Collect(Vec<(Type, &'a [u8])>),
    Supply(std::vec::IntoIter<String>),
}

impl<'a> ServerTextForms<'a> {
    pub(crate) fn collecting() -> Self {
        ServerTextForms::Collect(Vec::new())
    }

    pub(crate) fn supplying(texts: Vec<String>) -> Self {
        ServerTextForms::Supply(texts.into_iter())
    }

    /// The values the collecting pass needs converted (empty for a supplying one).
    pub(crate) fn take_requests(&mut self) -> Vec<(Type, &'a [u8])> {
        match self {
            ServerTextForms::Collect(requests) => std::mem::take(requests),
            ServerTextForms::Supply(_) => Vec::new(),
        }
    }

    pub(crate) fn text_form(&mut self, ty: &Type, raw: &'a [u8]) -> Result<Value, BoxError> {
        match self {
            ServerTextForms::Collect(requests) => {
                requests.push((ty.clone(), raw));
                Ok(Value::Null)
            }
            ServerTextForms::Supply(texts) => texts
                .next()
                .map(Value::Text)
                .ok_or_else(|| format!("no server text form for a {} value", ty.name()).into()),
        }
    }
}

pub fn decode_pg_row_values<'a>(
    row: &'a tokio_postgres::Row,
    forms: &mut ServerTextForms<'a>,
) -> Result<Vec<Value>, DriverError> {
    let columns = row.columns();
    let mut out = Vec::with_capacity(columns.len());
    for (index, column) in columns.iter().enumerate() {
        let ty = column.type_();
        let decode_error =
            |e: BoxError| DriverError::Conversion(format!("failed to decode column {} ({}): {}", index, ty.name(), e));
        let raw = row.try_get::<_, RawColumnBytes<'a>>(index).map_err(|e| decode_error(e.into()))?.0;
        out.push(match raw {
            Some(raw) => decode_value(ty, raw, forms).map_err(decode_error)?,
            None => Value::Null,
        });
    }
    Ok(out)
}

/// True when a column of type `ty` may need its values converted to text by the server (see
/// `ServerTextForms`) - an anonymous record may, whatever its fields turn out to be.
pub(crate) fn type_may_need_server_text_form(ty: &Type) -> bool {
    if decode_native_scalar(ty, &[]).is_some() {
        return false;
    }
    if ty.oid() == RECORD_OID {
        return true;
    }
    match ty.kind() {
        TypeShape::Array(inner) | TypeShape::Domain(inner) | TypeShape::Range(inner) | TypeShape::Multirange(inner) => {
            type_may_need_server_text_form(inner)
        }
        TypeShape::Enum(_) => false,
        TypeShape::Composite(fields) => fields.iter().any(|field| type_may_need_server_text_form(field.type_())),
        _ => !is_named_native_type(ty),
    }
}

/// Extension types (no fixed OID) this module decodes itself, matched by name.
pub(crate) fn is_named_native_type(ty: &Type) -> bool {
    matches!(ty.name(), "vector" | "citext" | "geometry" | "geography")
}

pub(crate) fn decode_value<'a>(ty: &Type, raw: &'a [u8], forms: &mut ServerTextForms<'a>) -> Result<Value, BoxError> {
    if let Some(value) = decode_native_scalar(ty, raw) {
        return value;
    }
    if ty.oid() == RECORD_OID {
        return decode_composite(raw, None, forms);
    }
    match ty.kind() {
        TypeShape::Array(member_type) => decode_array(member_type, raw, forms),
        TypeShape::Domain(base_type) => decode_value(base_type, raw, forms),
        TypeShape::Enum(_) => Ok(Value::Text(std::str::from_utf8(raw)?.to_string())),
        TypeShape::Range(_) => Ok(Value::Range(decode_range(ty, raw, forms)?)),
        TypeShape::Multirange(subtype) => decode_multirange(subtype, raw, forms),
        TypeShape::Composite(fields) => decode_composite(raw, Some(fields), forms),
        _ => match ty.name() {
            "vector" => Ok(PgVectorCell::from_sql(ty, raw)?.into_value()),
            "citext" => Ok(Value::Text(std::str::from_utf8(raw)?.to_string())),
            // PostGIS's binary form is EWKB; its text form is the same bytes in uppercase hex.
            "geometry" | "geography" => {
                let mut hex = String::with_capacity(raw.len() * 2);
                for byte in raw {
                    write!(hex, "{byte:02X}").expect("writing hex digits into a String never fails");
                }
                Ok(Value::Text(hex))
            }
            _ => forms.text_form(ty, raw),
        },
    }
}

/// Decodes a value of a fixed-OID built-in type this module has its own decoder for; `None` for
/// any other type. Every decoder answers (`Ok` or `Err`) for any payload, an empty one included -
/// `type_may_need_server_text_form` relies on that to tell the two apart.
pub(crate) fn decode_native_scalar(ty: &Type, raw: &[u8]) -> Option<Result<Value, BoxError>> {
    let utf8_text = |raw: &[u8]| -> Result<Value, BoxError> { Ok(Value::Text(std::str::from_utf8(raw)?.to_string())) };
    let unsigned_32 = |raw: &[u8]| -> Result<Value, BoxError> {
        let bytes: [u8; 4] =
            raw.try_into().map_err(|_| format!("invalid {} payload length {}", ty.name(), raw.len()))?;
        Ok(Value::Int(i64::from(u32::from_be_bytes(bytes))))
    };
    Some(match ty.oid() {
        16 => bool::from_sql(ty, raw).map(Value::Bool),
        21 => i16::from_sql(ty, raw).map(|v| Value::Int(v.into())),
        23 => i32::from_sql(ty, raw).map(|v| Value::Int(v.into())),
        20 => i64::from_sql(ty, raw).map(Value::Int),
        700 => f32::from_sql(ty, raw).map(|v| Value::Float(v.into())),
        701 => f64::from_sql(ty, raw).map(Value::Float),
        // text, varchar, bpchar, name, xml, cstring, refcursor, unknown - binary form is the text.
        25 | 1043 | 1042 | 19 | 142 | 2275 | 1790 | 705 => utf8_text(raw),
        // bytea, "char" (a single raw byte, returned as bytes like asyncpg).
        17 | 18 => Ok(Value::Bytes(raw.to_vec())),
        114 | 3802 => decode_json_text(ty, raw).map(Value::Text),
        2950 => uuid::Uuid::from_slice(raw).map(Value::Uuid).map_err(BoxError::from),
        1700 => decode_numeric(raw),
        1114 => decode_timestamp(raw).map(Value::Timestamp),
        1184 => decode_timestamp(raw).map(|v| Value::TimestampTz(v.and_utc())),
        1082 => decode_date(raw).map(Value::Date),
        1083 => i64::from_sql(&Type::INT8, raw)
            .map_err(|_| format!("invalid time payload length {}", raw.len()).into())
            .and_then(time_of_day)
            .map(Value::Time),
        1266 => PgTimeTzCell::from_sql(ty, raw).map(|cell| Value::TimeTz(cell.0, cell.1)),
        790 => decode_money(raw),
        1186 => decode_interval(raw).map(Value::Interval),
        650 | 869 => decode_network(ty, raw).map(Value::Network),
        829 | 774 => decode_macaddr(raw),
        1560 | 1562 => PgBitText::from_sql(ty, raw).map(|v| Value::Text(v.0)),
        3614 => PgTsVectorText::from_sql(ty, raw).map(|v| Value::Text(v.0)),
        3615 => PgTsQueryText::from_sql(ty, raw).map(|v| Value::Text(v.0)),
        // oid, xid, cid
        26 | 28 | 29 => unsigned_32(raw),
        // xid8
        5069 => <[u8; 8]>::try_from(raw).map_err(|_| format!("invalid xid8 payload length {}", raw.len()).into()).map(
            |bytes| {
                let value = u64::from_be_bytes(bytes);
                i64::try_from(value).map_or_else(|_| Value::BigInt(value.to_string()), Value::Int)
            },
        ),
        // void
        2278 => Ok(Value::Null),
        _ => return None,
    })
}
