//! Ranges and multiranges: one flags byte, then (unless empty) a length-prefixed lower bound (unless
//! -inf) and a length-prefixed upper bound (unless +inf).

use bytes::{BufMut, BytesMut};
use postgres_protocol::types::{empty_range_to_sql, range_from_sql, range_to_sql, Range, RangeBound};
use tokio_postgres::types::{IsNull, Kind as TypeShape, Type};

use crate::pg::decode_context::PgDecodeContext;
use crate::pg::parameter_encoding::{PgParameterEncoding, PgParameterMode};
use crate::pg::value::decode::{decode_value_with_context, ServerTextForms};
use crate::pg::value::{domain_base_type, BoxError, RangeValue, Value};
use crate::pg::wire_reader::PgWireReader;

/// A range type's subtype (`int4range` -> `int4`).
pub(crate) fn range_subtype(range_ty: &Type) -> Option<&Type> {
    match domain_base_type(range_ty).kind() {
        TypeShape::Range(subtype) => Some(subtype),
        _ => None,
    }
}

const RANGE_EMPTY: u8 = 0x01;
const RANGE_LB_INC: u8 = 0x02;
const RANGE_UB_INC: u8 = 0x04;

fn encode_range_bound(
    value: Option<&Value>,
    inclusive: bool,
    subtype: &Type,
    out: &mut BytesMut,
) -> Result<RangeBound<postgres_protocol::IsNull>, BoxError> {
    let Some(value) = value else {
        return Ok(RangeBound::Unbounded);
    };
    if let IsNull::Yes = PgParameterEncoding::new(value, subtype, PgParameterMode::BinaryRequired).encode(out)? {
        return Err("range bound encoded as SQL NULL, which range bounds cannot be".into());
    }
    Ok(if inclusive {
        RangeBound::Inclusive(postgres_protocol::IsNull::No)
    } else {
        RangeBound::Exclusive(postgres_protocol::IsNull::No)
    })
}

pub(crate) fn encode_range(range: &RangeValue, range_ty: &Type, out: &mut BytesMut) -> Result<IsNull, BoxError> {
    let subtype = range_subtype(range_ty).ok_or_else(|| -> BoxError {
        format!("cannot bind a range to a parameter of type {}", range_ty.name()).into()
    })?;
    if range.empty {
        empty_range_to_sql(out);
        return Ok(IsNull::No);
    }
    let tag_index = out.len();
    range_to_sql(
        |out| encode_range_bound(range.lower.as_deref(), range.lower_inc, subtype, out),
        |out| encode_range_bound(range.upper.as_deref(), range.upper_inc, subtype, out),
        out,
    )?;
    // Preserve declared inclusive flags even for unbounded sides; RangeBound canonicalizes them away.
    if range.lower_inc {
        out[tag_index] |= RANGE_LB_INC;
    }
    if range.upper_inc {
        out[tag_index] |= RANGE_UB_INC;
    }
    Ok(IsNull::No)
}

pub(crate) fn decode_range<'a>(
    range_ty: &Type,
    raw: &'a [u8],
    forms: &mut ServerTextForms<'a>,
    context: &mut PgDecodeContext,
) -> Result<RangeValue, BoxError> {
    let flags = *raw.first().ok_or("range payload is empty")?;
    let range = if flags & RANGE_EMPTY != 0 {
        if raw.len() != 1 {
            return Err("invalid range payload: trailing bytes".into());
        }
        Range::Empty
    } else {
        range_from_sql(raw)?
    };
    let Range::Nonempty(lower, upper) = range else {
        return Ok(RangeValue { lower: None, upper: None, lower_inc: false, upper_inc: false, empty: true });
    };
    let subtype = range_subtype(range_ty)
        .ok_or_else(|| -> BoxError { format!("unsupported range type {}", range_ty.name()).into() })?;
    let mut read_bound = |bound: RangeBound<Option<&'a [u8]>>| -> Result<Option<Box<Value>>, BoxError> {
        let bytes = match bound {
            RangeBound::Unbounded => return Ok(None),
            RangeBound::Inclusive(bytes) | RangeBound::Exclusive(bytes) => bytes.ok_or("range bound is NULL")?,
        };
        context.reserve_values(1)?;
        Ok(Some(Box::new(decode_value_with_context(subtype, bytes, forms, context)?)))
    };
    Ok(RangeValue {
        lower: read_bound(lower)?,
        upper: read_bound(upper)?,
        lower_inc: flags & RANGE_LB_INC != 0,
        upper_inc: flags & RANGE_UB_INC != 0,
        empty: false,
    })
}

/// A multirange (of ranges over `subtype`) as the list of its ranges: a range count, then each
/// range length-prefixed.
pub(crate) fn decode_multirange<'a>(
    subtype: &Type,
    raw: &'a [u8],
    forms: &mut ServerTextForms<'a>,
    context: &mut PgDecodeContext,
) -> Result<Value, BoxError> {
    let range_ty = Type::new(format!("{}range", subtype.name()), 0, TypeShape::Range(subtype.clone()), String::new());
    let range_ty = &range_ty;
    let mut reader = PgWireReader::new(raw, "multirange");
    let count = reader.count(5)?;
    context.reserve_values(count)?;
    let mut ranges = Vec::with_capacity(count);
    for _ in 0..count {
        let bytes = reader.nullable_bytes()?.ok_or("multirange member is NULL")?;
        ranges.push(Value::Range(context.visit(1, |context| decode_range(range_ty, bytes, forms, context))?));
    }
    reader.finish()?;
    Ok(Value::Array(ranges))
}

/// A list of ranges as a multirange of type `multirange_ty`: a range count, then each range
/// length-prefixed. The server merges overlapping and adjacent ranges itself.
pub(crate) fn encode_multirange(items: &[Value], multirange_ty: &Type, out: &mut BytesMut) -> Result<IsNull, BoxError> {
    let TypeShape::Multirange(subtype) = domain_base_type(multirange_ty).kind() else {
        return Err(format!("cannot bind a list of ranges to a parameter of type {}", multirange_ty.name()).into());
    };
    let range_ty = Type::new(format!("{}range", subtype.name()), 0, TypeShape::Range(subtype.clone()), String::new());
    out.put_i32(i32::try_from(items.len()).map_err(|_| "too many ranges in a multirange")?);
    for item in items {
        let Value::Range(range) = item else {
            return Err(format!(
                "cannot bind a {} value as a member of a {} parameter; every member must be a range",
                item.variant_type_name(),
                multirange_ty.name()
            )
            .into());
        };
        let mut buffer = BytesMut::new();
        encode_range(range, &range_ty, &mut buffer)?;
        out.put_i32(i32::try_from(buffer.len()).map_err(|_| "multirange member too large")?);
        out.extend_from_slice(&buffer);
    }
    Ok(IsNull::No)
}

#[cfg(test)]
mod tests {
    use super::*;
    use bytes::BytesMut;

    use tokio_postgres::types::Type;

    use crate::pg::types::datetime::*;

    use crate::pg::value::decode::ServerTextForms;
    use crate::pg::value::test_support::*;
    use crate::pg::value::{RangeValue, Value};

    #[test]
    fn decode_range_rejects_truncated_length_prefix() {
        // An inclusive lower bound, but only two of its four length-prefix bytes: return Err, not panic.
        let raw: &[u8] = &[RANGE_LB_INC, 0x00, 0x00];
        let result = decode_range(
            &Type::INT4_RANGE,
            raw,
            &mut ServerTextForms::collecting(),
            &mut PgDecodeContext::new(raw.len()),
        );
        assert!(result.is_err(), "expected Err for a truncated length prefix, got {result:?}");
    }

    #[test]
    fn decode_range_rejects_length_exceeding_remaining_bytes() {
        // A well-formed 4-byte length prefix claiming 100 bytes follow, but only 2 actually do.
        let mut raw = vec![RANGE_LB_INC];
        raw.extend_from_slice(&100i32.to_be_bytes());
        raw.extend_from_slice(&[0x00, 0x00]);
        let result = decode_range(
            &Type::INT4_RANGE,
            &raw,
            &mut ServerTextForms::collecting(),
            &mut PgDecodeContext::new(raw.len()),
        );
        assert!(result.is_err(), "expected Err when the claimed length exceeds the remaining bytes, got {result:?}");
    }

    #[test]
    fn decode_range_rejects_negative_length_prefix() {
        // The upper side is unbounded: a NULL lower bound must itself fail, not just a missing upper.
        for length in [-1_i32, -2, i32::MIN] {
            let mut raw = vec![RANGE_LB_INC | 0x10];
            raw.extend_from_slice(&length.to_be_bytes());
            let result = decode_range(
                &Type::INT4_RANGE,
                &raw,
                &mut ServerTextForms::collecting(),
                &mut PgDecodeContext::new(raw.len()),
            );
            assert!(result.is_err(), "expected Err for a negative length prefix, got {result:?}");
        }
    }

    #[test]
    fn range_inclusive_flags_round_trip_even_for_unbounded_sides() {
        for lower_bounded in [false, true] {
            for upper_bounded in [false, true] {
                for (lower_inc, upper_inc) in [(false, false), (false, true), (true, false), (true, true)] {
                    let range = RangeValue {
                        lower: lower_bounded.then(|| Box::new(Value::Int(10))),
                        upper: upper_bounded.then(|| Box::new(Value::Int(20))),
                        lower_inc,
                        upper_inc,
                        empty: false,
                    };
                    // Appending must patch the range's tag, not an earlier value's bytes.
                    let mut out = BytesMut::from(&b"prefix"[..]);
                    encode_range(&range, &Type::INT4_RANGE, &mut out).expect("must encode");
                    assert_eq!(&out[..6], b"prefix");
                    let raw = &out[6..];
                    let expected_tag = (u8::from(lower_inc) * 0x02)
                        | (u8::from(upper_inc) * 0x04)
                        | (u8::from(!lower_bounded) * 0x08)
                        | (u8::from(!upper_bounded) * 0x10);
                    assert_eq!(raw[0], expected_tag);
                    let decoded = decode_range(
                        &Type::INT4_RANGE,
                        raw,
                        &mut ServerTextForms::collecting(),
                        &mut PgDecodeContext::new(raw.len()),
                    )
                    .expect("must decode");
                    assert_eq!(decoded.lower_inc, lower_inc);
                    assert_eq!(decoded.upper_inc, upper_inc);
                    assert_eq!(decoded.lower.is_some(), lower_bounded);
                    assert_eq!(decoded.upper.is_some(), upper_bounded);
                    assert!(!decoded.empty);
                }
            }
        }
    }

    #[test]
    fn range_flags_keep_empty_and_unknown_bits_behavior() {
        for (raw, empty) in [(&[0x9e][..], false), (&[0x9f][..], true)] {
            let range = decode_range(
                &Type::INT4_RANGE,
                raw,
                &mut ServerTextForms::collecting(),
                &mut PgDecodeContext::new(raw.len()),
            )
            .expect("unknown bits must not change recognized flags");
            assert_eq!(range.empty, empty);
            assert!(range.lower.is_none() && range.upper.is_none());
            assert_eq!(range.lower_inc, !empty);
            assert_eq!(range.upper_inc, !empty);
        }
        for raw in [&[][..], &[0x01, 0x00][..], &[0x18, 0x00][..]] {
            assert!(decode_range(
                &Type::INT4_RANGE,
                raw,
                &mut ServerTextForms::collecting(),
                &mut PgDecodeContext::new(raw.len()),
            )
            .is_err());
        }
    }

    #[test]
    fn range_bounds_require_non_null_binary_values() {
        for value in [Value::Null, Value::Text("10".into())] {
            let range = RangeValue {
                lower: Some(Box::new(value)),
                upper: None,
                lower_inc: true,
                upper_inc: false,
                empty: false,
            };
            assert!(encode_range(&range, &Type::INT4_RANGE, &mut BytesMut::new()).is_err());
        }
    }

    #[test]
    fn decode_range_still_accepts_a_well_formed_payload() {
        // Sanity check the fix didn't also break the ordinary case: [10, 20) as an int4range.
        let mut raw = vec![RANGE_LB_INC];
        raw.extend_from_slice(&4i32.to_be_bytes());
        raw.extend_from_slice(&10i32.to_be_bytes());
        raw.extend_from_slice(&4i32.to_be_bytes());
        raw.extend_from_slice(&20i32.to_be_bytes());
        let result = decode_range(
            &Type::INT4_RANGE,
            &raw,
            &mut ServerTextForms::collecting(),
            &mut PgDecodeContext::new(raw.len()),
        )
        .expect("well-formed payload must still decode");
        assert!(result.lower_inc);
        assert!(!result.upper_inc);
        match result.lower.as_deref() {
            Some(Value::Int(10)) => {}
            other => panic!("expected lower bound Int(10), got {other:?}"),
        }
        match result.upper.as_deref() {
            Some(Value::Int(20)) => {}
            other => panic!("expected upper bound Int(20), got {other:?}"),
        }
        assert!(
            decode_range(&Type::INT4_RANGE, &raw, &mut ServerTextForms::collecting(), &mut PgDecodeContext::new(0),)
                .is_err(),
            "decoding both bounds must consume the shared value budget"
        );
    }
    #[test]
    fn python_extreme_range_bounds_encode_as_their_moments() {
        let range = RangeValue {
            lower: Some(Box::new(Value::TimestampTz(python_min_datetime().and_utc()))),
            upper: Some(Box::new(Value::TimestampTz(python_max_datetime().and_utc()))),
            lower_inc: true,
            upper_inc: false,
            empty: false,
        };
        let mut out = BytesMut::new();
        encode_range(&range, &Type::TSTZ_RANGE, &mut out).expect("must encode");
        let decoded = decode_range(
            &Type::TSTZ_RANGE,
            &out,
            &mut ServerTextForms::collecting(),
            &mut PgDecodeContext::new(out.len()),
        )
        .expect("must decode");
        let mut expected = vec![RANGE_LB_INC];
        expected.extend_from_slice(&8i32.to_be_bytes());
        expected.extend_from_slice(&(-730_119i64 * MICROSECONDS_PER_DAY).to_be_bytes());
        expected.extend_from_slice(&8i32.to_be_bytes());
        expected.extend_from_slice(&(2_921_940i64 * MICROSECONDS_PER_DAY - 1).to_be_bytes());
        assert_eq!(out.to_vec(), expected);
        assert!(
            matches!(decoded.lower.as_deref(), Some(Value::TimestampTz(value)) if value.naive_utc() == python_min_datetime())
        );
        assert!(
            matches!(decoded.upper.as_deref(), Some(Value::TimestampTz(value)) if value.naive_utc() == python_max_datetime())
        );
    }
    #[test]
    fn multiranges_decode_as_range_lists() {
        // One member: 17-byte range, inclusive lower bound, two four-byte int32 bounds.
        let raw =
            b"\x00\x00\x00\x01\x00\x00\x00\x11\x02\x00\x00\x00\x04\x00\x00\x00\x01\x00\x00\x00\x04\x00\x00\x00\x03";
        match decode(&Type::INT4MULTI_RANGE, raw) {
            Value::Array(items) => {
                assert!(matches!(&items[..], [Value::Range(range)]
                        if matches!(range.lower.as_deref(), Some(Value::Int(1)))
                        && matches!(range.upper.as_deref(), Some(Value::Int(3)))
                        && range.lower_inc && !range.upper_inc && !range.empty));
            }
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn range_lists_encode_as_multiranges() {
        let range = |lower: i64, upper: i64| {
            Value::Range(RangeValue {
                lower: Some(Box::new(Value::Int(lower))),
                upper: Some(Box::new(Value::Int(upper))),
                lower_inc: true,
                upper_inc: false,
                empty: false,
            })
        };
        let mut out = BytesMut::new();
        encode_multirange(&[range(1, 3), range(5, 7)], &Type::INT4MULTI_RANGE, &mut out).expect("must encode");
        let expected = [
            0, 0, 0, 2, 0, 0, 0, 17, 2, 0, 0, 0, 4, 0, 0, 0, 1, 0, 0, 0, 4, 0, 0, 0, 3, 0, 0, 0, 17, 2, 0, 0, 0, 4, 0,
            0, 0, 5, 0, 0, 0, 4, 0, 0, 0, 7,
        ];
        assert_eq!(out.as_ref(), expected.as_slice());
        match decode(&Type::INT4MULTI_RANGE, &out) {
            Value::Array(items) => {
                assert_eq!(items.len(), 2);
                for (item, (lower, upper)) in items.iter().zip([(1, 3), (5, 7)]) {
                    assert!(matches!(item, Value::Range(range)
                        if matches!(range.lower.as_deref(), Some(Value::Int(value)) if *value == lower)
                        && matches!(range.upper.as_deref(), Some(Value::Int(value)) if *value == upper)
                        && range.lower_inc && !range.upper_inc && !range.empty));
                }
            }
            other => panic!("unexpected {other:?}"),
        }
        let mut out = BytesMut::new();
        assert!(encode_multirange(&[Value::Int(1)], &Type::INT4MULTI_RANGE, &mut out).is_err());
        assert!(encode_multirange(&[range(1, 3)], &Type::INT4_ARRAY, &mut out).is_err());
    }
}
