//! Ranges and multiranges: one flags byte, then (unless empty) a length-prefixed lower bound (unless
//! -inf) and a length-prefixed upper bound (unless +inf).

use bytes::{Buf, BufMut, BytesMut};
use tokio_postgres::types::{IsNull, Kind as TypeShape, Type};

use crate::pg::types::read_length_prefixed;
use crate::pg::value::decode::{decode_value, ServerTextForms};
use crate::pg::value::{domain_base_type, BoxError, RangeValue, Value};

/// A range type's subtype (`int4range` -> `int4`).
pub(crate) fn range_subtype(range_ty: &Type) -> Option<&Type> {
    match domain_base_type(range_ty).kind() {
        TypeShape::Range(subtype) => Some(subtype),
        _ => None,
    }
}

// Postgres range binary wire format (range_send/range_recv): one flags byte, then (unless empty)
// a length-prefixed lower bound (unless -inf) and a length-prefixed upper bound (unless +inf).
pub(crate) const RANGE_EMPTY: u8 = 0x01;
pub(crate) const RANGE_LB_INC: u8 = 0x02;
pub(crate) const RANGE_UB_INC: u8 = 0x04;
pub(crate) const RANGE_LB_INF: u8 = 0x08;
pub(crate) const RANGE_UB_INF: u8 = 0x10;

pub(crate) fn encode_range_bound(value: &Value, subtype: &Type, out: &mut BytesMut) -> Result<(), BoxError> {
    let mut buf = BytesMut::new();
    if let IsNull::Yes = value.encode(subtype, &mut buf, false)? {
        return Err("range bound encoded as SQL NULL, which range bounds cannot be".into());
    }
    out.put_i32(i32::try_from(buf.len()).map_err(|_| "range bound too large")?);
    out.extend_from_slice(&buf);
    Ok(())
}

pub(crate) fn encode_range(r: &RangeValue, range_ty: &Type, out: &mut BytesMut) -> Result<IsNull, BoxError> {
    let subtype = range_subtype(range_ty).ok_or_else(|| -> BoxError {
        format!("cannot bind a range to a parameter of type {}", range_ty.name()).into()
    })?;
    if r.empty {
        out.put_u8(RANGE_EMPTY);
        return Ok(IsNull::No);
    }
    let mut flags = 0u8;
    if r.lower_inc {
        flags |= RANGE_LB_INC;
    }
    if r.upper_inc {
        flags |= RANGE_UB_INC;
    }
    if r.lower.is_none() {
        flags |= RANGE_LB_INF;
    }
    if r.upper.is_none() {
        flags |= RANGE_UB_INF;
    }
    out.put_u8(flags);
    if let Some(lower) = &r.lower {
        encode_range_bound(lower, subtype, out)?;
    }
    if let Some(upper) = &r.upper {
        encode_range_bound(upper, subtype, out)?;
    }
    Ok(IsNull::No)
}

pub(crate) fn decode_range<'a>(
    range_ty: &Type,
    mut raw: &'a [u8],
    forms: &mut ServerTextForms<'a>,
) -> Result<RangeValue, BoxError> {
    let flags = raw.try_get_u8().map_err(|_| "empty range payload")?;
    if flags & RANGE_EMPTY != 0 {
        return Ok(RangeValue { lower: None, upper: None, lower_inc: false, upper_inc: false, empty: true });
    }
    let subtype = range_subtype(range_ty)
        .ok_or_else(|| -> BoxError { format!("unsupported range type {}", range_ty.name()).into() })?;
    let mut read_bound = |raw: &mut &'a [u8]| -> Result<Box<Value>, BoxError> {
        let bytes = read_length_prefixed(raw, "range bound")?.ok_or("range bound is NULL")?;
        Ok(Box::new(decode_value(subtype, bytes, forms)?))
    };
    let lower = if flags & RANGE_LB_INF == 0 { Some(read_bound(&mut raw)?) } else { None };
    let upper = if flags & RANGE_UB_INF == 0 { Some(read_bound(&mut raw)?) } else { None };
    Ok(RangeValue {
        lower,
        upper,
        lower_inc: flags & RANGE_LB_INC != 0,
        upper_inc: flags & RANGE_UB_INC != 0,
        empty: false,
    })
}

/// A multirange (of ranges over `subtype`) as the list of its ranges: a range count, then each
/// range length-prefixed.
pub(crate) fn decode_multirange<'a>(
    subtype: &Type,
    mut raw: &'a [u8],
    forms: &mut ServerTextForms<'a>,
) -> Result<Value, BoxError> {
    let range_ty = Type::new(format!("{}range", subtype.name()), 0, TypeShape::Range(subtype.clone()), String::new());
    let range_ty = &range_ty;
    let count = raw.try_get_i32().map_err(|_| "empty multirange payload")?;
    let count = usize::try_from(count).map_err(|_| format!("negative multirange count {count}"))?;
    let mut ranges = Vec::with_capacity(count.min(raw.len()));
    for _ in 0..count {
        let bytes = read_length_prefixed(&mut raw, "multirange member")?.ok_or("multirange member is NULL")?;
        ranges.push(Value::Range(decode_range(range_ty, bytes, forms)?));
    }
    Ok(Value::Array(ranges))
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
        // flags (RANGE_LB_INC, not empty/inf on either side) then only 2 of the 4 length-prefix
        // bytes a real lower-bound length would need - try_get_i32() must return Err, not panic
        // via the raw Buf::get_i32() this used to call.
        let raw: &[u8] = &[RANGE_LB_INC, 0x00, 0x00];
        let result = decode_range(&Type::INT4_RANGE, raw, &mut ServerTextForms::collecting());
        assert!(result.is_err(), "expected Err for a truncated length prefix, got {result:?}");
    }

    #[test]
    fn decode_range_rejects_length_exceeding_remaining_bytes() {
        // A well-formed 4-byte length prefix claiming 100 bytes follow, but only 2 actually do.
        let mut raw = vec![RANGE_LB_INC];
        raw.extend_from_slice(&100i32.to_be_bytes());
        raw.extend_from_slice(&[0x00, 0x00]);
        let result = decode_range(&Type::INT4_RANGE, &raw, &mut ServerTextForms::collecting());
        assert!(result.is_err(), "expected Err when the claimed length exceeds the remaining bytes, got {result:?}");
    }

    #[test]
    fn decode_range_rejects_negative_length_prefix() {
        // A length prefix that reads as a negative i32 (0xFFFFFFFF = -1) - the old
        // `as usize` cast would silently wrap this into a huge value instead of erroring.
        let mut raw = vec![RANGE_LB_INC];
        raw.extend_from_slice(&(-1i32).to_be_bytes());
        let result = decode_range(&Type::INT4_RANGE, &raw, &mut ServerTextForms::collecting());
        assert!(result.is_err(), "expected Err for a negative length prefix, got {result:?}");
    }

    #[test]
    fn decode_range_still_accepts_a_well_formed_payload() {
        // Sanity check the fix didn't also break the ordinary case: [10, 20) as an int4range.
        let mut raw = vec![RANGE_LB_INC];
        raw.extend_from_slice(&4i32.to_be_bytes());
        raw.extend_from_slice(&10i32.to_be_bytes());
        raw.extend_from_slice(&4i32.to_be_bytes());
        raw.extend_from_slice(&20i32.to_be_bytes());
        let result = decode_range(&Type::INT4_RANGE, &raw, &mut ServerTextForms::collecting())
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
    }
    #[test]
    fn infinite_range_bounds_encode_back_to_infinity() {
        let range = RangeValue {
            lower: Some(Box::new(Value::TimestampTz(python_min_datetime().and_utc()))),
            upper: Some(Box::new(Value::TimestampTz(python_max_datetime().and_utc()))),
            lower_inc: true,
            upper_inc: false,
            empty: false,
        };
        let mut out = BytesMut::new();
        encode_range(&range, &Type::TSTZ_RANGE, &mut out).expect("must encode");
        let decoded = decode_range(&Type::TSTZ_RANGE, &out, &mut ServerTextForms::collecting()).expect("must decode");
        let mut expected = vec![RANGE_LB_INC];
        expected.extend_from_slice(&8i32.to_be_bytes());
        expected.extend_from_slice(&i64::MIN.to_be_bytes());
        expected.extend_from_slice(&8i32.to_be_bytes());
        expected.extend_from_slice(&i64::MAX.to_be_bytes());
        assert_eq!(out.to_vec(), expected);
        assert!(
            matches!(decoded.lower.as_deref(), Some(Value::TimestampTz(v)) if v.naive_utc() == python_min_datetime())
        );
    }
    #[test]
    fn multiranges_decode_as_range_lists() {
        let range = Value::Range(RangeValue {
            lower: Some(Box::new(Value::Int(1))),
            upper: Some(Box::new(Value::Int(3))),
            lower_inc: true,
            upper_inc: false,
            empty: false,
        });
        let range_raw = encode(&range, &Type::INT4_RANGE).unwrap();
        let mut raw = 1i32.to_be_bytes().to_vec();
        raw.extend_from_slice(&(range_raw.len() as i32).to_be_bytes());
        raw.extend_from_slice(&range_raw);
        match decode(&Type::INT4MULTI_RANGE, &raw) {
            Value::Array(items) => {
                assert!(matches!(&items[..], [Value::Range(r)] if matches!(r.lower.as_deref(), Some(Value::Int(1)))));
            }
            other => panic!("unexpected {other:?}"),
        }
    }
}
