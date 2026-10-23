//! `NUMERIC`: exact decimal text <-> the binary format, and its non-finite values.

use crate::pg::value::{BoxError, Value};
use bytes::Buf;
use std::fmt::Write as _;

mod decimal_digits;
mod non_finite_numeric;

pub(crate) use decimal_digits::DecimalDigits;
pub use non_finite_numeric::NonFiniteNumeric;

/// NUMERIC's wire sign word for a negative value.
pub(crate) const NUMERIC_NEGATIVE_SIGN: u16 = 0x4000;

/// The most fractional digits (display scale) a NUMERIC can carry.
pub(crate) const NUMERIC_MAX_DISPLAY_SCALE: i64 = 0x3FFF;

/// The most digits a NUMERIC can carry before the decimal point.
pub(crate) const NUMERIC_MAX_INTEGER_DIGITS: i64 = 131_072;

/// Decodes a binary NUMERIC into the exact text `numeric_out` prints for it (or a non-finite value).
pub(crate) fn decode_numeric(mut raw: &[u8]) -> Result<Value, BoxError> {
    if raw.len() < 8 {
        return Err(format!("invalid numeric payload length {}", raw.len()).into());
    }
    let group_count = raw.get_i16();
    let weight = i64::from(raw.get_i16());
    let sign = raw.get_u16();
    let display_scale = raw.get_u16();
    if let Some(special) = NonFiniteNumeric::from_sign(sign) {
        return Ok(Value::NonFiniteDecimal(special));
    }
    if sign != 0 && sign != NUMERIC_NEGATIVE_SIGN {
        return Err(format!("invalid numeric sign {sign:#06x}").into());
    }
    let declared_group_count = usize::try_from(group_count).ok().filter(|&count| raw.len() == count * 2);
    let Some(group_count) = declared_group_count else {
        return Err(
            format!("numeric payload declares {group_count} digit group(s) but carries {} byte(s)", raw.len()).into()
        );
    };
    let mut groups = Vec::with_capacity(group_count);
    for _ in 0..group_count {
        let group = raw.get_i16();
        if !(0..10_000).contains(&group) {
            return Err(format!("invalid numeric digit group {group}").into());
        }
        groups.push(group);
    }
    let group_at =
        |index: i64| -> i16 { usize::try_from(index).ok().and_then(|index| groups.get(index)).copied().unwrap_or(0) };
    let mut text = String::new();
    if sign == NUMERIC_NEGATIVE_SIGN && !groups.is_empty() {
        text.push('-');
    }
    if weight < 0 {
        text.push('0');
    } else {
        for index in 0..=weight {
            let group = group_at(index);
            if index == 0 {
                write!(text, "{group}").expect("writing into a String never fails");
            } else {
                write!(text, "{group:04}").expect("writing into a String never fails");
            }
        }
    }
    if display_scale > 0 {
        text.push('.');
        let fraction_start = text.len();
        let mut index = weight + 1;
        while text.len() - fraction_start < display_scale as usize {
            write!(text, "{:04}", group_at(index)).expect("writing into a String never fails");
            index += 1;
        }
        text.truncate(fraction_start + display_scale as usize);
    }
    Ok(Value::Decimal(text))
}

#[cfg(test)]
mod tests {
    use super::*;
    use bytes::BytesMut;

    use tokio_postgres::types::{ToSql, Type};

    use crate::pg::value::test_support::*;
    use crate::pg::value::Value;

    fn decimal_round_trip(text: &str) -> String {
        let raw = encode(&Value::Decimal(text.to_string()), &Type::NUMERIC).expect("must encode");
        match decode(&Type::NUMERIC, &raw) {
            Value::Decimal(decoded) => decoded,
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn numeric_keeps_every_digit_beyond_28_significant_ones() {
        assert_eq!(decimal_round_trip("1000000000.00000000000000000000"), "1000000000.00000000000000000000");
        assert_eq!(decimal_round_trip("1.123456789012345678901234567890"), "1.123456789012345678901234567890");
        assert_eq!(
            decimal_round_trip("-123456789012345678901234567890123456789.5"),
            "-123456789012345678901234567890123456789.5"
        );
        assert_eq!(decimal_round_trip("1E-40"), "0.0000000000000000000000000000000000000001");
        assert_eq!(decimal_round_trip("1E+30"), "1000000000000000000000000000000");
        assert_eq!(decimal_round_trip("0.000"), "0.000");
        assert_eq!(decimal_round_trip("-0"), "0");
        assert_eq!(decimal_round_trip("12.5"), "12.5");
        assert_eq!(decimal_round_trip("99990000.0001"), "99990000.0001");
    }

    #[test]
    fn numeric_binary_matches_the_postgres_layout() {
        // 12.5: ndigits 2, weight 0, sign +, dscale 1, digits [12, 5000].
        let raw = encode(&Value::Decimal("12.5".to_string()), &Type::NUMERIC).expect("must encode");
        assert_eq!(raw, vec![0, 2, 0, 0, 0, 0, 0, 1, 0, 12, 0x13, 0x88]);
    }

    #[test]
    fn numeric_rejects_values_beyond_its_limits() {
        assert!(encode(&Value::Decimal("1E+200000".to_string()), &Type::NUMERIC).is_err());
        assert!(encode(&Value::Decimal("1E-20000".to_string()), &Type::NUMERIC).is_err());
        assert!(encode(&Value::Decimal("abc".to_string()), &Type::NUMERIC).is_err());
    }

    #[test]
    fn float_bound_to_numeric_is_its_exact_decimal_expansion() {
        assert_eq!(DecimalDigits::from_f64(0.5), DecimalDigits::parse("0.5").unwrap());
        assert_eq!(DecimalDigits::from_f64(1e22), DecimalDigits::parse("10000000000000000000000").unwrap());
        assert_eq!(
            DecimalDigits::from_f64(0.1),
            DecimalDigits::parse("0.1000000000000000055511151231257827021181583404541015625").unwrap()
        );
        assert_eq!(DecimalDigits::from_f64(-2.0), DecimalDigits::parse("-2").unwrap());
    }

    #[test]
    fn big_int_binds_to_numeric_and_float_but_not_to_integer_types() {
        let big = Value::BigInt("1180591620717411303424".to_string());
        assert!(encode(&big, &Type::NUMERIC).is_ok());
        assert_eq!(encode(&big, &Type::FLOAT8).unwrap(), 1.180_591_620_717_411_3e21_f64.to_be_bytes().to_vec());
        assert!(encode(&big, &Type::INT8).unwrap_err().contains("out of range"));
        assert!(encode(&big, &Type::INT4).unwrap_err().contains("out of range"));
        assert!(encode(&big, &Type::DATE).is_err());
    }
    #[test]
    fn non_finite_numeric_round_trips_through_its_wire_format() {
        for special in [NonFiniteNumeric::NaN, NonFiniteNumeric::PositiveInfinity, NonFiniteNumeric::NegativeInfinity] {
            let mut out = BytesMut::new();
            Value::NonFiniteDecimal(special).to_sql(&Type::NUMERIC, &mut out).expect("must encode");
            match decode(&Type::NUMERIC, &out) {
                Value::NonFiniteDecimal(decoded) => assert_eq!(decoded, special),
                other => panic!("expected {special:?}, got {other:?}"),
            }
        }
    }
}
