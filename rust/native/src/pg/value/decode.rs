//! Wire bytes of a result column -> `Value`.

use std::fmt::Write as _;

use tokio_postgres::types::{FromSql, Kind as TypeShape, Type};

use crate::pg::error::DriverError;
use crate::pg::types::array::decode_array;
use crate::pg::types::bit_string::PgBitText;
use crate::pg::types::composite::decode_composite;
use crate::pg::types::datetime::time_of_day;
use crate::pg::types::interval::decode_interval;
use crate::pg::types::mac_address::decode_macaddr;
use crate::pg::types::money::decode_money;
use crate::pg::types::network::decode_network;
use crate::pg::types::numeric::decode_numeric;
use crate::pg::types::range::{decode_multirange, decode_range};
use crate::pg::types::text_search_query::PgTsQueryText;
use crate::pg::types::text_search_vector::PgTsVectorText;
use crate::pg::types::time_with_zone::PgTimeTzCell;
use crate::pg::types::vector::PgVectorCell;
use crate::pg::value::wire_scalar::WireScalar;
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

    pub(crate) fn text_form(&mut self, postgres_type: &Type, raw: &'a [u8]) -> Result<Value, BoxError> {
        match self {
            ServerTextForms::Collect(requests) => {
                requests.push((postgres_type.clone(), raw));
                Ok(Value::Null)
            }
            ServerTextForms::Supply(texts) => texts
                .next()
                .map(Value::Text)
                .ok_or_else(|| format!("no server text form for a {} value", postgres_type.name()).into()),
        }
    }
}

/// The error of a value of column `index`, of type `ty`, that doesn't decode.
pub(crate) fn get_decode_error(index: usize, postgres_type: &Type, error: &BoxError) -> DriverError {
    DriverError::Conversion(format!("failed to decode column {} ({}): {}", index, postgres_type.name(), error))
}

/// The wire bytes of column `index` of `row` - None for NULL.
pub(crate) fn get_raw_column(row: &tokio_postgres::Row, index: usize) -> Result<Option<&[u8]>, DriverError> {
    let postgres_type = row.columns()[index].type_();
    row.try_get::<_, RawColumnBytes<'_>>(index)
        .map(|raw| raw.0)
        .map_err(|error| get_decode_error(index, postgres_type, &error.into()))
}

pub fn decode_pg_row_values<'a>(
    row: &'a tokio_postgres::Row,
    forms: &mut ServerTextForms<'a>,
) -> Result<Vec<Value>, DriverError> {
    let columns = row.columns();
    let mut out = Vec::with_capacity(columns.len());
    for (index, column) in columns.iter().enumerate() {
        let postgres_type = column.type_();
        out.push(match get_raw_column(row, index)? {
            Some(raw) => decode_value(postgres_type, raw, forms)
                .map_err(|error| get_decode_error(index, postgres_type, &error))?,
            None => Value::Null,
        });
    }
    Ok(out)
}

/// Checks that every value of `row` decodes - with the error decoding it would give - without
/// keeping a decoded value: a common type is read in place, any other decoded and dropped. Only
/// for a row whose columns need no server text form.
pub(crate) fn check_pg_row_values(row: &tokio_postgres::Row) -> Result<(), DriverError> {
    for (index, column) in row.columns().iter().enumerate() {
        let postgres_type = column.type_();
        let Some(raw) = get_raw_column(row, index)? else {
            continue;
        };
        let checked = match WireScalar::parse(postgres_type, raw) {
            Some(scalar) => scalar.map(drop),
            None => decode_value(postgres_type, raw, &mut ServerTextForms::collecting()).map(drop),
        };
        checked.map_err(|error| get_decode_error(index, postgres_type, &error))?;
    }
    Ok(())
}

/// True when a column of type `ty` may need its values converted to text by the server (see
/// `ServerTextForms`) - an anonymous record may, whatever its fields turn out to be.
pub(crate) fn type_may_need_server_text_form(postgres_type: &Type) -> bool {
    if decode_native_scalar(postgres_type, &[]).is_some() {
        return false;
    }
    if postgres_type.oid() == RECORD_OID {
        return true;
    }
    match postgres_type.kind() {
        TypeShape::Array(inner) | TypeShape::Domain(inner) | TypeShape::Range(inner) | TypeShape::Multirange(inner) => {
            type_may_need_server_text_form(inner)
        }
        TypeShape::Enum(_) => false,
        TypeShape::Composite(fields) => fields.iter().any(|field| type_may_need_server_text_form(field.type_())),
        _ => !is_named_native_type(postgres_type),
    }
}

/// Extension types (no fixed OID) this module decodes itself, matched by name.
pub(crate) fn is_named_native_type(postgres_type: &Type) -> bool {
    matches!(postgres_type.name(), "vector" | "citext" | "geometry" | "geography")
}

pub(crate) fn decode_value<'a>(
    postgres_type: &Type,
    raw: &'a [u8],
    forms: &mut ServerTextForms<'a>,
) -> Result<Value, BoxError> {
    if let Some(value) = decode_native_scalar(postgres_type, raw) {
        return value;
    }
    if postgres_type.oid() == RECORD_OID {
        return decode_composite(raw, None, forms);
    }
    match postgres_type.kind() {
        TypeShape::Array(member_type) => decode_array(member_type, raw, forms),
        TypeShape::Domain(base_type) => decode_value(base_type, raw, forms),
        TypeShape::Enum(_) => Ok(Value::Text(std::str::from_utf8(raw)?.to_string())),
        TypeShape::Range(_) => Ok(Value::Range(decode_range(postgres_type, raw, forms)?)),
        TypeShape::Multirange(subtype) => decode_multirange(subtype, raw, forms),
        TypeShape::Composite(fields) => decode_composite(raw, Some(fields), forms),
        _ => match postgres_type.name() {
            "vector" => Ok(PgVectorCell::from_sql(postgres_type, raw)?.into_value()),
            "citext" => Ok(Value::Text(std::str::from_utf8(raw)?.to_string())),
            // PostGIS's binary form is EWKB; its text form is the same bytes in uppercase hex.
            "geometry" | "geography" => {
                let mut hex = String::with_capacity(raw.len() * 2);
                for byte in raw {
                    write!(hex, "{byte:02X}").expect("writing hex digits into a String never fails");
                }
                Ok(Value::Text(hex))
            }
            _ => forms.text_form(postgres_type, raw),
        },
    }
}

/// Decodes a value of a fixed-OID built-in type this module has its own decoder for; `None` for
/// any other type. Every decoder answers (`Ok` or `Err`) for any payload, an empty one included -
/// `type_may_need_server_text_form` relies on that to tell the two apart.
pub(crate) fn decode_native_scalar(postgres_type: &Type, raw: &[u8]) -> Option<Result<Value, BoxError>> {
    if let Some(scalar) = WireScalar::parse(postgres_type, raw) {
        return Some(scalar.map(WireScalar::into_value));
    }
    let unsigned_32 = |raw: &[u8]| -> Result<Value, BoxError> {
        let bytes: [u8; 4] =
            raw.try_into().map_err(|_| format!("invalid {} payload length {}", postgres_type.name(), raw.len()))?;
        Ok(Value::Int(i64::from(u32::from_be_bytes(bytes))))
    };
    Some(match postgres_type.oid() {
        // bytea, "char" (a single raw byte, returned as bytes like asyncpg).
        17 | 18 => Ok(Value::Bytes(raw.to_vec())),
        1700 => decode_numeric(raw),
        1083 => i64::from_sql(&Type::INT8, raw)
            .map_err(|_| format!("invalid time payload length {}", raw.len()).into())
            .and_then(time_of_day)
            .map(Value::Time),
        1266 => PgTimeTzCell::from_sql(postgres_type, raw).map(|cell| Value::TimeTz(cell.0, cell.1)),
        790 => decode_money(raw),
        1186 => decode_interval(raw).map(Value::Interval),
        650 | 869 => decode_network(postgres_type, raw).map(Value::Network),
        829 | 774 => decode_macaddr(raw),
        1560 | 1562 => PgBitText::from_sql(postgres_type, raw).map(|value| Value::Text(value.0)),
        3614 => PgTsVectorText::from_sql(postgres_type, raw).map(|value| Value::Text(value.0)),
        3615 => PgTsQueryText::from_sql(postgres_type, raw).map(|value| Value::Text(value.0)),
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
