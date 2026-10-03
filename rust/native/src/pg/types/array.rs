//! Arrays of any dimension: the binary format, and a text literal for a list of strings bound to an
//! array of a type whose binary format is not its text.

use bytes::BytesMut;
use fallible_iterator::FallibleIterator;
use tokio_postgres::types::{IsNull, Kind as TypeShape, Type};

use crate::pg::value::decode::{decode_value, ServerTextForms};
use crate::pg::value::{domain_base_type, BoxError, Value};

/// True when `member_type`'s binary wire format is its own text, so a string can be written
/// as-is even where a binary encoding is required (an array element, a range bound, COPY).
pub(crate) fn member_type_is_text_compatible(member_type: &Type) -> bool {
    let member_type = domain_base_type(member_type);
    matches!(
        *member_type,
        Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::NAME | Type::UNKNOWN | Type::JSON | Type::JSONB | Type::UUID
    ) || matches!(member_type.kind(), TypeShape::Enum(_))
        || member_type.name() == "citext"
}

/// True when `value` is a list of strings (NULLs and nested lists of them allowed, at least one
/// string) bound to an array of a type whose binary wire format is not its text - pgvector
/// `vector`, `PostGIS` `geography`, `inet`, ... Such a parameter is sent as a text-format array
/// literal instead, so the server parses each element with that type's own input function.
pub(crate) fn is_text_format_array(value: &Value, ty: &Type) -> bool {
    fn only_text_leaves(items: &[Value], has_text: &mut bool) -> bool {
        items.iter().all(|item| match item {
            Value::Text(_) => {
                *has_text = true;
                true
            }
            Value::Null => true,
            Value::Array(sub_items) => only_text_leaves(sub_items, has_text),
            _ => false,
        })
    }
    let Value::Array(items) = value else {
        return false;
    };
    let TypeShape::Array(member_type) = domain_base_type(ty).kind() else {
        return false;
    };
    if member_type_is_text_compatible(member_type) {
        return false;
    }
    let mut has_text = false;
    only_text_leaves(items, &mut has_text) && has_text
}

/// Writes Postgres's text array literal (`{"a",NULL,{"b"}}`) for a list checked by
/// `is_text_format_array` - every string double-quoted, with `"` and `\` backslash-escaped.
pub(crate) fn write_text_array_literal(items: &[Value], out: &mut String) {
    out.push('{');
    for (index, item) in items.iter().enumerate() {
        if index > 0 {
            out.push(',');
        }
        match item {
            Value::Array(sub_items) => write_text_array_literal(sub_items, out),
            Value::Text(text) => {
                out.push('"');
                for character in text.chars() {
                    if character == '"' || character == '\\' {
                        out.push('\\');
                    }
                    out.push(character);
                }
                out.push('"');
            }
            _ => out.push_str("NULL"),
        }
    }
    out.push('}');
}

/// Encodes a list (nested lists for more dimensions, e.g. `[[1, 2], [3, 4]]`) as ONE Postgres
/// array value: a header with the dimension sizes, then the leaf elements in row-major order,
/// each encoded in the member type's binary format.
pub(crate) fn encode_array(items: &[Value], ty: &Type, out: &mut BytesMut) -> Result<IsNull, BoxError> {
    let TypeShape::Array(member_type) = ty.kind() else {
        return Err(format!("cannot bind a list to non-array column type {}", ty.name()).into());
    };
    let (dims, leaves) = array_shape_and_leaves(items).map_err(|reason| -> BoxError {
        format!(
            "cannot bind a multi-dimensional array parameter: {reason} (Postgres itself requires every \
             sub-array at the same nesting depth to have identical length)"
        )
        .into()
    })?;
    if !member_type_is_text_compatible(member_type) && leaves.iter().any(|leaf| matches!(leaf, Value::Text(_))) {
        return Err(format!(
            "cannot bind a string as an element of a {} array - array elements have no separate \
             text-format encoding path (only a top-level scalar parameter of this type can be \
             text-encoded); pass a value of the column's native type instead",
            member_type.name()
        )
        .into());
    }
    let mut dimensions = Vec::with_capacity(dims.len());
    for len in dims {
        let len = i32::try_from(len)
            .map_err(|_| format!("array dimension of {len} elements exceeds Postgres's INT4 limit"))?;
        dimensions.push(postgres_protocol::types::ArrayDimension { len, lower_bound: 1 });
    }
    postgres_protocol::types::array_to_sql(
        dimensions,
        member_type.oid(),
        leaves,
        |leaf, buf| match leaf.encode(member_type, buf, false)? {
            IsNull::No => Ok(postgres_protocol::IsNull::No),
            IsNull::Yes => Ok(postgres_protocol::IsNull::Yes),
        },
        out,
    )?;
    Ok(IsNull::No)
}

/// Computes the per-dimension sizes and the flat, row-major leaf values of a (possibly nested)
/// list - or reports why the nesting is not a valid rectangular array: a sub-array whose length
/// differs from a sibling's, or a scalar/NULL element beside sibling sub-arrays.
pub(crate) fn array_shape_and_leaves(items: &[Value]) -> Result<(Vec<usize>, Vec<&Value>), String> {
    if items.iter().all(|item| !matches!(item, Value::Array(_))) {
        return Ok((vec![items.len()], items.iter().collect()));
    }
    let mut sub_shape: Option<Vec<usize>> = None;
    let mut leaves = Vec::new();
    for item in items {
        let Value::Array(sub_items) = item else {
            return Err("a scalar or NULL element sits alongside a sub-array at the same nesting depth - every \
                 element at a given depth must itself be a sub-array of the same shape"
                .to_string());
        };
        let (shape, mut sub_leaves) = array_shape_and_leaves(sub_items)?;
        match &sub_shape {
            None => sub_shape = Some(shape),
            Some(expected) if *expected == shape => {}
            Some(expected) => {
                return Err(format!(
                    "sub-array of shape {shape:?} does not match a sibling sub-array's shape {expected:?}"
                ));
            }
        }
        leaves.append(&mut sub_leaves);
    }
    let mut full_shape = vec![items.len()];
    full_shape.extend(sub_shape.expect("items is non-empty in this branch"));
    Ok((full_shape, leaves))
}

/// Rebuilds the nested `Value::Array` tree from wire-order dimension sizes and the flat,
/// row-major leaves.
pub(crate) fn nest_leaves(dims: &[i32], leaves: Vec<Value>) -> Value {
    match dims {
        [] | [_] => Value::Array(leaves),
        [outer, rest @ ..] => {
            let chunk_size: usize = rest.iter().map(|&len| len as usize).product();
            let mut remaining = leaves;
            let mut sub_arrays = Vec::with_capacity(*outer as usize);
            // Exactly `outer` chunks, even when `chunk_size` is 0.
            for _ in 0..*outer {
                let chunk_tail = remaining.split_off(chunk_size.min(remaining.len()));
                sub_arrays.push(nest_leaves(rest, remaining));
                remaining = chunk_tail;
            }
            Value::Array(sub_arrays)
        }
    }
}

/// Decodes an array of any dimensionality into nested `Value::Array`s, each element through
/// `decode_value` with the array's member type.
pub(crate) fn decode_array<'a>(
    member_type: &Type,
    raw: &'a [u8],
    forms: &mut ServerTextForms<'a>,
) -> Result<Value, BoxError> {
    let array = postgres_protocol::types::array_from_sql(raw)?;
    let mut dims = Vec::new();
    let mut dimensions = array.dimensions();
    while let Some(dimension) = dimensions.next()? {
        dims.push(dimension.len);
    }
    // ndim == 0 is Postgres's representation of every empty array, whatever its shape was.
    if dims.is_empty() {
        return Ok(Value::Array(vec![]));
    }
    let mut leaves = Vec::new();
    let mut values = array.values();
    while let Some(element) = values.next()? {
        leaves.push(match element {
            Some(bytes) => decode_value(member_type, bytes, forms)?,
            None => Value::Null,
        });
    }
    Ok(nest_leaves(&dims, leaves))
}

#[cfg(test)]
mod tests {
    use bytes::BytesMut;

    use tokio_postgres::types::{Format, Kind as TypeShape, ToSql, Type};

    use crate::pg::value::test_support::*;
    use crate::pg::value::Value;

    #[test]
    fn array_of_text_into_a_binary_typed_array_column_is_sent_as_a_text_literal() {
        // A 6-character string used to be written as MACADDR's 6 raw binary bytes - now the whole
        // array is a text-format literal the server parses element by element.
        let value = Value::Array(vec![Value::Text("abcdef".to_string()), Value::Null]);
        assert!(matches!(value.encode_format(&Type::MACADDR_ARRAY), Format::Text));
        let mut out = BytesMut::new();
        value.to_sql(&Type::MACADDR_ARRAY, &mut out).expect("a text array literal must encode");
        assert_eq!(&out[..], b"{\"abcdef\",NULL}");
    }

    #[test]
    fn text_array_literal_escapes_quotes_and_backslashes_and_nests() {
        let value = Value::Array(vec![
            Value::Array(vec![Value::Text("a\"b".to_string()), Value::Text("c\\d".to_string())]),
            Value::Array(vec![Value::Text("[1,2]".to_string()), Value::Null]),
        ]);
        let mut out = BytesMut::new();
        value.to_sql(&vector_array_type(), &mut out).expect("a nested text array literal must encode");
        assert_eq!(&out[..], b"{{\"a\\\"b\",\"c\\\\d\"},{\"[1,2]\",NULL}}");
    }

    #[test]
    fn array_mixing_text_and_native_values_into_a_binary_typed_array_column_is_rejected() {
        let value = Value::Array(vec![Value::Text("abcdef".to_string()), Value::Int(1)]);
        assert!(matches!(value.encode_format(&Type::MACADDR_ARRAY), Format::Binary));
        assert!(value.to_sql(&Type::MACADDR_ARRAY, &mut BytesMut::new()).is_err());
    }

    #[test]
    fn array_of_text_into_a_citext_array_column_is_binary_encoded_as_plain_text() {
        let citext = Type::new("citext".to_string(), 16390, TypeShape::Simple, "public".to_string());
        let citext_array = Type::new("_citext".to_string(), 16395, TypeShape::Array(citext), "public".to_string());
        let value = Value::Array(vec![Value::Text("Abc".to_string())]);
        assert!(matches!(value.encode_format(&citext_array), Format::Binary));
        value.to_sql(&citext_array, &mut BytesMut::new()).expect("a citext array of strings must encode");
    }
    #[test]
    fn array_of_text_into_a_text_typed_array_column_still_works() {
        let value = Value::Array(vec![Value::Text("hello".to_string())]);
        let mut out = BytesMut::new();
        let result = value.to_sql(&Type::TEXT_ARRAY, &mut out);
        assert!(
            result.is_ok(),
            "expected text array encoding to still succeed, got {:?}",
            result.err().map(|e| e.to_string())
        );
    }

    #[test]
    fn list_bound_to_a_scalar_parameter_is_an_error_not_a_panic() {
        let value = Value::Array(vec![Value::Int(1)]);
        let mut out = BytesMut::new();
        assert!(value.to_sql(&Type::INT4, &mut out).is_err());
        let nested = Value::Array(vec![Value::Array(vec![Value::Int(1)])]);
        assert!(nested.to_sql(&Type::TIMESTAMPTZ, &mut BytesMut::new()).is_err());
    }

    #[test]
    fn list_bound_to_a_jsonb_parameter_is_encoded_as_a_json_array() {
        let value = Value::Array(vec![Value::Int(1), Value::Text("a".to_string()), Value::Null]);
        let mut out = BytesMut::new();
        value.to_sql(&Type::JSONB, &mut out).expect("a list must encode as jsonb");
        assert_eq!(&out[..], b"[1,\"a\",null]");
    }
}

#[cfg(test)]
mod encode_tests {
    use bytes::{Buf, BytesMut};

    use tokio_postgres::types::{IsNull, Kind as TypeShape, ToSql, Type};

    use crate::pg::value::Value;

    /// Parses the exact wire layout `encode_nd_array`/`postgres_protocol::types::array_to_sql`
    /// produce - `ndim`, `has_nulls`, `element_type_oid`, `ndim` pairs of `(len, lower_bound)`,
    /// then the flat row-major elements (each a 4-byte length prefix, `-1` for NULL, else that
    /// many raw bytes) - by hand, matching this file's own established test style of building/
    /// reading raw Postgres wire bytes directly rather than pulling in another crate.
    struct DecodedNdArray {
        dims: Vec<(i32, i32)>,
        has_nulls: bool,
        // `None` for a NULL element, `Some(i32)` for an INT4 element decoded from its 4 raw bytes.
        elements: Vec<Option<i32>>,
    }

    fn decode_int_nd_array(mut raw: &[u8]) -> DecodedNdArray {
        let ndim = raw.get_i32();
        let has_nulls = raw.get_i32() != 0;
        let _element_type_oid = raw.get_u32();
        let dims: Vec<(i32, i32)> = (0..ndim).map(|_| (raw.get_i32(), raw.get_i32())).collect();
        let mut elements = Vec::new();
        while raw.has_remaining() {
            let len = raw.get_i32();
            if len < 0 {
                elements.push(None);
            } else {
                let mut value_bytes = &raw[..len as usize];
                elements.push(Some(value_bytes.get_i32()));
                raw.advance(len as usize);
            }
        }
        DecodedNdArray { dims, has_nulls, elements }
    }

    fn int_value(v: i64) -> Value {
        Value::Int(v)
    }

    #[test]
    fn encodes_a_rectangular_2d_array_in_row_major_order() {
        // [[1, 2], [3, 4]] - the exact shape from the bug report.
        let value = Value::Array(vec![
            Value::Array(vec![int_value(1), int_value(2)]),
            Value::Array(vec![int_value(3), int_value(4)]),
        ]);
        let mut out = BytesMut::new();
        let result = value.to_sql(&Type::INT4_ARRAY, &mut out).expect("rectangular 2D array must encode");
        assert!(matches!(result, IsNull::No));
        let decoded = decode_int_nd_array(&out);
        assert_eq!(decoded.dims, vec![(2, 1), (2, 1)]);
        assert!(!decoded.has_nulls);
        assert_eq!(decoded.elements, vec![Some(1), Some(2), Some(3), Some(4)]);
    }

    #[test]
    fn encodes_a_rectangular_3d_array_in_row_major_order() {
        // [[[1,2],[3,4]], [[5,6],[7,8]]]
        let sub = |a: i64, b: i64| Value::Array(vec![int_value(a), int_value(b)]);
        let value =
            Value::Array(vec![Value::Array(vec![sub(1, 2), sub(3, 4)]), Value::Array(vec![sub(5, 6), sub(7, 8)])]);
        let mut out = BytesMut::new();
        value.to_sql(&Type::INT4_ARRAY, &mut out).expect("rectangular 3D array must encode");
        let decoded = decode_int_nd_array(&out);
        assert_eq!(decoded.dims, vec![(2, 1), (2, 1), (2, 1)]);
        assert_eq!(decoded.elements, (1..=8).map(Some).collect::<Vec<_>>());
    }

    #[test]
    fn ragged_sub_arrays_of_differing_length_are_a_clean_error_not_a_panic() {
        // [[1, 2], [3]] - Postgres itself rejects this shape for ARRAY[...] literals.
        let value =
            Value::Array(vec![Value::Array(vec![int_value(1), int_value(2)]), Value::Array(vec![int_value(3)])]);
        let mut out = BytesMut::new();
        let result = value.to_sql(&Type::INT4_ARRAY, &mut out);
        assert!(result.is_err(), "expected a clean Err for a ragged array, got {:?}", result.map(|_| ()));
    }

    #[test]
    fn a_scalar_sibling_next_to_a_sub_array_is_a_clean_error_not_a_panic() {
        // [[1, 2], 3] - one sibling is a sub-array, the other is a bare scalar.
        let value = Value::Array(vec![Value::Array(vec![int_value(1), int_value(2)]), int_value(3)]);
        let mut out = BytesMut::new();
        let result = value.to_sql(&Type::INT4_ARRAY, &mut out);
        assert!(result.is_err(), "expected a clean Err, got {:?}", result.map(|_| ()));
    }

    #[test]
    fn a_null_sibling_next_to_a_sub_array_is_a_clean_error_not_a_panic() {
        // [[1, 2], NULL] - a whole "row" can't be NULL independent of its siblings' shape.
        let value = Value::Array(vec![Value::Array(vec![int_value(1), int_value(2)]), Value::Null]);
        let mut out = BytesMut::new();
        let result = value.to_sql(&Type::INT4_ARRAY, &mut out);
        assert!(result.is_err(), "expected a clean Err, got {:?}", result.map(|_| ()));
    }

    #[test]
    fn encodes_a_2d_array_of_empty_rows() {
        // [[], []] - each row is an empty sub-array, but both rows agree on that empty shape.
        let value = Value::Array(vec![Value::Array(vec![]), Value::Array(vec![])]);
        let mut out = BytesMut::new();
        value.to_sql(&Type::INT4_ARRAY, &mut out).expect("2D array of empty rows must encode");
        let decoded = decode_int_nd_array(&out);
        assert_eq!(decoded.dims, vec![(2, 1), (0, 1)]);
        assert!(decoded.elements.is_empty());
    }

    #[test]
    fn encodes_a_2d_array_with_null_leaf_elements_and_sets_the_has_nulls_flag() {
        // [[1, NULL], [3, 4]]
        let value = Value::Array(vec![
            Value::Array(vec![int_value(1), Value::Null]),
            Value::Array(vec![int_value(3), int_value(4)]),
        ]);
        let mut out = BytesMut::new();
        value.to_sql(&Type::INT4_ARRAY, &mut out).expect("2D array with a NULL leaf must encode");
        let decoded = decode_int_nd_array(&out);
        assert!(decoded.has_nulls);
        assert_eq!(decoded.elements, vec![Some(1), None, Some(3), Some(4)]);
    }

    #[test]
    fn a_plain_1d_array_still_uses_the_unchanged_existing_path() {
        // No Value::Array elements at all - must behave exactly as before this fix (ndim=1).
        let value = Value::Array(vec![int_value(1), int_value(2), int_value(3)]);
        let mut out = BytesMut::new();
        value.to_sql(&Type::INT4_ARRAY, &mut out).expect("plain 1D array must still encode");
        let decoded = decode_int_nd_array(&out);
        assert_eq!(decoded.dims, vec![(3, 1)]);
        assert_eq!(decoded.elements, vec![Some(1), Some(2), Some(3)]);
    }

    #[test]
    fn int4_array_type_shape_is_a_single_level_not_nested_regardless_of_actual_data_shape() {
        // Confirms the assumption encode_nd_array's own doc comment relies on: Postgres has no
        // per-dimension catalog type, so INT4_ARRAY's TypeShape::Array member is always the plain
        // scalar INT4 - never TypeShape::Array(TypeShape::Array(INT4)) - no matter how deeply nested the
        // actual value being bound against it is.
        match Type::INT4_ARRAY.kind() {
            TypeShape::Array(member) => assert_eq!(*member, Type::INT4),
            other => panic!("expected TypeShape::Array(INT4), got {other:?}"),
        }
    }

    #[test]
    fn nested_array_against_a_non_array_column_type_is_a_clean_error_not_a_panic() {
        let value = Value::Array(vec![Value::Array(vec![int_value(1), int_value(2)])]);
        let mut out = BytesMut::new();
        let result = value.to_sql(&Type::INT4, &mut out);
        assert!(result.is_err(), "expected a clean Err, got {:?}", result.map(|_| ()));
    }
}

#[cfg(test)]
mod decode_tests {
    use super::*;
    use bytes::BytesMut;

    use tokio_postgres::types::{ToSql, Type};

    use crate::pg::value::decode::{decode_value, ServerTextForms};

    use crate::pg::value::Value;

    /// A `Value` tree collapsed to just its shape/leaf-values, ignoring which exact `Value`
    /// variant a leaf is - lets a test assert on the decoded structure without a long manual
    /// `match` per test.
    #[derive(Debug, PartialEq)]
    enum Shape {
        Leaf(Option<i64>),
        Nested(Vec<Shape>),
    }

    fn to_shape(value: &Value) -> Shape {
        match value {
            Value::Array(items) => Shape::Nested(items.iter().map(to_shape).collect()),
            Value::Int(n) => Shape::Leaf(Some(*n)),
            Value::Null => Shape::Leaf(None),
            other => panic!("unexpected leaf variant in this test: {other:?}"),
        }
    }

    #[test]
    fn round_trips_a_rectangular_2d_array_through_encode_then_decode() {
        let original = Value::Array(vec![
            Value::Array(vec![Value::Int(1), Value::Int(2)]),
            Value::Array(vec![Value::Int(3), Value::Int(4)]),
        ]);
        let mut out = BytesMut::new();
        original.to_sql(&Type::INT4_ARRAY, &mut out).expect("must encode");
        let decoded = decode_value(&Type::INT4_ARRAY, &out, &mut ServerTextForms::collecting()).expect("must decode");
        assert_eq!(to_shape(&decoded), to_shape(&original));
    }

    #[test]
    fn round_trips_a_rectangular_3d_array_through_encode_then_decode() {
        let sub = |a: i64, b: i64| Value::Array(vec![Value::Int(a), Value::Int(b)]);
        let original =
            Value::Array(vec![Value::Array(vec![sub(1, 2), sub(3, 4)]), Value::Array(vec![sub(5, 6), sub(7, 8)])]);
        let mut out = BytesMut::new();
        original.to_sql(&Type::INT4_ARRAY, &mut out).expect("must encode");
        let decoded = decode_value(&Type::INT4_ARRAY, &out, &mut ServerTextForms::collecting()).expect("must decode");
        assert_eq!(to_shape(&decoded), to_shape(&original));
    }

    #[test]
    fn round_trips_a_2d_array_with_null_leaves() {
        let original = Value::Array(vec![
            Value::Array(vec![Value::Int(1), Value::Null]),
            Value::Array(vec![Value::Int(3), Value::Int(4)]),
        ]);
        let mut out = BytesMut::new();
        original.to_sql(&Type::INT4_ARRAY, &mut out).expect("must encode");
        let decoded = decode_value(&Type::INT4_ARRAY, &out, &mut ServerTextForms::collecting()).expect("must decode");
        assert_eq!(to_shape(&decoded), to_shape(&original));
    }

    #[test]
    fn decodes_a_plain_1d_array_unchanged() {
        let original = Value::Array(vec![Value::Int(1), Value::Int(2), Value::Int(3)]);
        let mut out = BytesMut::new();
        original.to_sql(&Type::INT4_ARRAY, &mut out).expect("must encode");
        let decoded = decode_value(&Type::INT4_ARRAY, &out, &mut ServerTextForms::collecting()).expect("must decode");
        assert_eq!(to_shape(&decoded), to_shape(&original));
    }

    #[test]
    fn decodes_a_zero_dimension_empty_array() {
        // ndim=0, flags=0, element_type_oid, no dimension entries, no elements - Postgres's own
        // wire representation of `{}`/`[]`.
        let mut raw = Vec::new();
        raw.extend_from_slice(&0i32.to_be_bytes());
        raw.extend_from_slice(&0i32.to_be_bytes());
        raw.extend_from_slice(&(Type::INT4.oid()).to_be_bytes());
        let decoded = decode_value(&Type::INT4_ARRAY, &raw, &mut ServerTextForms::collecting()).expect("must decode");
        match decoded {
            Value::Array(items) => assert!(items.is_empty()),
            other => panic!("expected an empty Value::Array, got {other:?}"),
        }
    }

    #[test]
    fn decode_array_with_a_mismatched_member_type_is_a_clean_error_not_a_panic() {
        let value = Value::Array(vec![Value::Array(vec![Value::Int(1)])]);
        let mut out = BytesMut::new();
        value.to_sql(&Type::INT4_ARRAY, &mut out).expect("must encode");
        let result = decode_array(&Type::INT4_ARRAY, &out, &mut ServerTextForms::collecting());
        assert!(result.is_err(), "expected a clean Err, got {:?}", result.map(|_| ()));
    }
}
