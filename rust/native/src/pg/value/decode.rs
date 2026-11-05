//! Wire bytes of a result column -> `Value`.

use std::fmt::Write as _;

use tokio_postgres::types::{FromSql, Kind as TypeShape, Type};

use crate::pg::decode_context::PgDecodeContext;
use crate::pg::error::DriverError;
use crate::pg::types::array::decode_array;
use crate::pg::types::composite::decode_composite;
use crate::pg::types::range::{decode_multirange, decode_range};
use crate::pg::types::vector::PgVectorCell;
use crate::pg::value::column_decoder::PgColumnDecoder;
pub(crate) use crate::pg::value::server_text_forms::ServerTextForms;
use crate::pg::value::{BoxError, Value};

/// The anonymous `record` pseudo-type (`ROW(...)`).
pub(crate) const RECORD_OID: u32 = 2249;
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
    decoders: &[PgColumnDecoder<'_>],
    forms: &mut ServerTextForms<'a>,
) -> Result<Vec<Value>, DriverError> {
    decoders
        .iter()
        .enumerate()
        .map(|(index, decoder)| {
            decoder
                .decode(get_raw_column(row, index)?, forms)
                .map_err(|error| get_decode_error(index, row.columns()[index].type_(), &error))
        })
        .collect()
}

#[cfg(test)]
pub(crate) fn decode_value<'a>(
    postgres_type: &Type,
    raw: &'a [u8],
    forms: &mut ServerTextForms<'a>,
) -> Result<Value, BoxError> {
    let mut context = PgDecodeContext::new(raw.len());
    decode_value_with_context(postgres_type, raw, forms, &mut context)
}

pub(crate) fn decode_value_with_context<'a>(
    postgres_type: &Type,
    raw: &'a [u8],
    forms: &mut ServerTextForms<'a>,
    context: &mut PgDecodeContext,
) -> Result<Value, BoxError> {
    PgColumnDecoder::new(postgres_type).decode_with_context(raw, forms, context)
}

pub(super) fn decode_structured_value<'a>(
    postgres_type: &Type,
    raw: &'a [u8],
    forms: &mut ServerTextForms<'a>,
    context: &mut PgDecodeContext,
) -> Result<Value, BoxError> {
    if postgres_type.oid() == RECORD_OID {
        return decode_composite(raw, None, forms, context);
    }
    match postgres_type.kind() {
        TypeShape::Array(member_type) => decode_array(member_type, raw, forms, context),
        TypeShape::Enum(_) => Ok(Value::Text(std::str::from_utf8(raw)?.to_string())),
        TypeShape::Range(_) => Ok(Value::Range(decode_range(postgres_type, raw, forms, context)?)),
        TypeShape::Multirange(subtype) => decode_multirange(subtype, raw, forms, context),
        TypeShape::Composite(fields) => decode_composite(raw, Some(fields), forms, context),
        _ => match postgres_type.name() {
            "vector" => {
                let value = PgVectorCell::from_sql(postgres_type, raw)?.into_value();
                if let Value::Array(items) = &value {
                    context.reserve_values(items.len())?;
                }
                Ok(value)
            }
            "citext" => Ok(Value::Text(std::str::from_utf8(raw)?.to_string())),
            // PostGIS's binary form is EWKB; its text form is the same bytes in uppercase hex.
            "geometry" | "geography" => {
                let mut hex = String::with_capacity(raw.len() * 2);
                for byte in raw {
                    write!(hex, "{byte:02X}").expect("writing hex digits into a String never fails");
                }
                Ok(Value::Text(hex))
            }
            _ => Ok(forms.text_form(postgres_type, raw)),
        },
    }
}

#[cfg(test)]
mod nested_wire_tests {
    use super::*;
    use crate::pg::types::constants::MAX_DECODE_NESTING;

    fn record(child_oid: u32, child: &[u8]) -> Vec<u8> {
        let mut raw = 1_i32.to_be_bytes().to_vec();
        raw.extend_from_slice(&child_oid.to_be_bytes());
        raw.extend_from_slice(&i32::try_from(child.len()).unwrap().to_be_bytes());
        raw.extend_from_slice(child);
        raw
    }

    #[test]
    fn nested_records_share_the_native_depth_boundary() {
        let mut raw = 0_i32.to_be_bytes().to_vec();
        for _ in 0..32 {
            raw = record(RECORD_OID, &raw);
        }
        let value = decode_value(&Type::RECORD, &raw, &mut ServerTextForms::collecting()).unwrap();
        let mut cursor = &value;
        for _ in 0..32 {
            let Value::Composite(children) = cursor else { panic!("expected a record") };
            assert_eq!(children.len(), 1);
            cursor = &children[0];
        }
        assert!(matches!(cursor, Value::Composite(children) if children.is_empty()));
        for _ in 32..MAX_DECODE_NESTING {
            raw = record(RECORD_OID, &raw);
        }
        let error = decode_value(&Type::RECORD, &raw, &mut ServerTextForms::collecting()).unwrap_err();
        assert!(error.to_string().contains("nesting depth"));
    }

    #[test]
    fn nested_payloads_must_finish_and_every_truncation_is_an_error() {
        let inner = record(Type::INT4.oid(), &7_i32.to_be_bytes());
        let raw = record(RECORD_OID, &inner);
        for length in 0..raw.len() {
            assert!(decode_value(&Type::RECORD, &raw[..length], &mut ServerTextForms::collecting()).is_err());
        }
        let mut trailing = inner;
        trailing.push(0);
        let raw = record(RECORD_OID, &trailing);
        assert!(decode_value(&Type::RECORD, &raw, &mut ServerTextForms::collecting()).is_err());
    }

    #[test]
    fn array_members_share_the_enclosing_record_context() {
        let mut array = 1_i32.to_be_bytes().to_vec();
        array.extend_from_slice(&0_i32.to_be_bytes());
        array.extend_from_slice(&Type::INT4.oid().to_be_bytes());
        array.extend_from_slice(&2_i32.to_be_bytes());
        array.extend_from_slice(&1_i32.to_be_bytes());
        for number in [7_i32, 11] {
            array.extend_from_slice(&4_i32.to_be_bytes());
            array.extend_from_slice(&number.to_be_bytes());
        }
        let raw = record(Type::INT4_ARRAY.oid(), &array);
        let value = decode_value(&Type::RECORD, &raw, &mut ServerTextForms::collecting()).unwrap();
        let Value::Composite(fields) = value else { panic!("expected a record") };
        let Value::Array(items) = &fields[0] else { panic!("expected an array") };
        assert!(matches!(items.as_slice(), [Value::Int(7), Value::Int(11)]));
        array.push(0);
        let raw = record(Type::INT4_ARRAY.oid(), &array);
        assert!(decode_value(&Type::RECORD, &raw, &mut ServerTextForms::collecting()).is_err());
    }
}
