//! Composite values (`ROW(...)`), read as tuples.

use tokio_postgres::types::{Field, Kind as TypeShape, Type};

use crate::pg::decode_context::PgDecodeContext;
use crate::pg::value::decode::{decode_value_with_context, ServerTextForms};
use crate::pg::value::{BoxError, Value};
use crate::pg::wire_reader::PgWireReader;

/// A composite value (`ROW(...)`) as a tuple of its fields: a field count, then per field its type
/// OID and length-prefixed bytes. `fields` carries the declared field types of a named composite
/// type; an anonymous record's fields are typed by the OIDs on the wire.
pub(crate) fn decode_composite<'a>(
    raw: &'a [u8],
    fields: Option<&[Field]>,
    forms: &mut ServerTextForms<'a>,
    context: &mut PgDecodeContext,
) -> Result<Value, BoxError> {
    let mut reader = PgWireReader::new(raw, "composite");
    let count = reader.count(8)?;
    if fields.is_some_and(|fields| fields.len() != count) {
        return Err("composite field count differs from its declared type".into());
    }
    context.reserve_values(count)?;
    let mut values = Vec::with_capacity(count);
    for index in 0..count {
        let oid = reader.u32()?;
        let Some(bytes) = reader.nullable_bytes()? else {
            values.push(Value::Null);
            continue;
        };
        let field_type = match fields.and_then(|fields| fields.get(index)) {
            Some(field) => field.type_().clone(),
            None => {
                Type::from_oid(oid).unwrap_or_else(|| Type::new(String::new(), oid, TypeShape::Simple, String::new()))
            }
        };
        values.push(decode_value_with_context(&field_type, bytes, forms, context)?);
    }
    reader.finish()?;
    Ok(Value::Composite(values))
}
