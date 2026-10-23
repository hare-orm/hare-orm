//! Composite values (`ROW(...)`), read as tuples.

use bytes::Buf;
use tokio_postgres::types::{Field, Kind as TypeShape, Type};

use crate::pg::types::read_length_prefixed;
use crate::pg::value::decode::{decode_value, ServerTextForms};
use crate::pg::value::{BoxError, Value};

/// A composite value (`ROW(...)`) as a tuple of its fields: a field count, then per field its type
/// OID and length-prefixed bytes. `fields` carries the declared field types of a named composite
/// type; an anonymous record's fields are typed by the OIDs on the wire.
pub(crate) fn decode_composite<'a>(
    mut raw: &'a [u8],
    fields: Option<&[Field]>,
    forms: &mut ServerTextForms<'a>,
) -> Result<Value, BoxError> {
    let count = raw.try_get_i32().map_err(|_| "empty composite payload")?;
    let count = usize::try_from(count).map_err(|_| format!("negative composite field count {count}"))?;
    let mut values = Vec::with_capacity(count.min(raw.len()));
    for index in 0..count {
        let oid = raw.try_get_u32().map_err(|_| "truncated composite field type")?;
        let Some(bytes) = read_length_prefixed(&mut raw, "composite field")? else {
            values.push(Value::Null);
            continue;
        };
        let field_type = match fields.and_then(|fields| fields.get(index)) {
            Some(field) => field.type_().clone(),
            None => {
                Type::from_oid(oid).unwrap_or_else(|| Type::new(String::new(), oid, TypeShape::Simple, String::new()))
            }
        };
        values.push(decode_value(&field_type, bytes, forms)?);
    }
    Ok(Value::Composite(values))
}
