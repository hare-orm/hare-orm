//! `NUMERIC`: exact decimal text <-> the binary format, and its non-finite values.

use std::fmt::Write as _;

use bytes::{Buf, BufMut, BytesMut};
use tokio_postgres::types::IsNull;

use crate::pg::value::{BoxError, Value};

/// NUMERIC's wire sign word for a negative value.
pub(crate) const NUMERIC_NEGATIVE_SIGN: u16 = 0x4000;
/// The most fractional digits (display scale) a NUMERIC can carry.
pub(crate) const NUMERIC_MAX_DISPLAY_SCALE: i64 = 0x3FFF;
/// The most digits a NUMERIC can carry before the decimal point.
pub(crate) const NUMERIC_MAX_INTEGER_DIGITS: i64 = 131_072;

/// A finite decimal number as `digits * 10^exponent` - no precision limit of its own.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct DecimalDigits {
    negative: bool,
    /// Decimal digits (each 0-9), most significant first, without leading zeros; empty for zero.
    digits: Vec<u8>,
    exponent: i64,
}

impl DecimalDigits {
    /// Parses decimal text in `str(decimal.Decimal)`/`numeric_out` form (`-12.50`, `1E+5`, `3e-7`).
    pub(crate) fn parse(text: &str) -> Result<Self, String> {
        let invalid = || format!("invalid decimal {text:?}");
        let (negative, unsigned) = match text.as_bytes().first() {
            Some(b'-') => (true, &text[1..]),
            Some(b'+') => (false, &text[1..]),
            _ => (false, text),
        };
        let (mantissa, exponent_text) = match unsigned.find(['e', 'E']) {
            Some(position) => (&unsigned[..position], Some(&unsigned[position + 1..])),
            None => (unsigned, None),
        };
        let mut exponent: i64 = match exponent_text {
            Some(exponent_text) => exponent_text.parse().map_err(|_| invalid())?,
            None => 0,
        };
        let mut digits = Vec::with_capacity(mantissa.len());
        let mut seen_point = false;
        for byte in mantissa.bytes() {
            match byte {
                b'0'..=b'9' => {
                    digits.push(byte - b'0');
                    if seen_point {
                        exponent = exponent.checked_sub(1).ok_or_else(invalid)?;
                    }
                }
                b'.' if !seen_point => seen_point = true,
                _ => return Err(invalid()),
            }
        }
        if digits.is_empty() {
            return Err(invalid());
        }
        let leading_zeros = digits.iter().take_while(|&&digit| digit == 0).count();
        digits.drain(..leading_zeros);
        Ok(DecimalDigits { negative, digits, exponent })
    }

    /// The exact decimal value of a finite float, as `decimal.Decimal(float)` gives it.
    pub(crate) fn from_f64(value: f64) -> Self {
        let bits = value.to_bits();
        let negative = bits >> 63 == 1;
        let exponent_bits = ((bits >> 52) & 0x7FF) as i64;
        let fraction = bits & ((1 << 52) - 1);
        let (mut mantissa, mut binary_exponent) =
            if exponent_bits == 0 { (fraction, -1074) } else { (fraction | (1 << 52), exponent_bits - 1075) };
        if mantissa == 0 {
            return DecimalDigits { negative, digits: Vec::new(), exponent: 0 };
        }
        while mantissa & 1 == 0 && binary_exponent < 0 {
            mantissa >>= 1;
            binary_exponent += 1;
        }
        // Little-endian base-10^9 limbs.
        let mut limbs: Vec<u64> = vec![
            mantissa % 1_000_000_000,
            mantissa / 1_000_000_000 % 1_000_000_000,
            mantissa / 1_000_000_000_000_000_000,
        ];
        let multiply = |limbs: &mut Vec<u64>, factor: u64| {
            let mut carry = 0u64;
            for limb in limbs.iter_mut() {
                let product = *limb * factor + carry;
                *limb = product % 1_000_000_000;
                carry = product / 1_000_000_000;
            }
            while carry > 0 {
                limbs.push(carry % 1_000_000_000);
                carry /= 1_000_000_000;
            }
        };
        let (factor_base, mut remaining, exponent) = if binary_exponent >= 0 {
            (2u64, binary_exponent, 0)
        } else {
            // m * 2^-k == m * 5^k * 10^-k
            (5u64, -binary_exponent, binary_exponent)
        };
        while remaining > 0 {
            let step = remaining.min(13);
            multiply(&mut limbs, factor_base.pow(step as u32));
            remaining -= step;
        }
        let mut text = String::new();
        for (index, limb) in limbs.iter().rev().enumerate() {
            if index == 0 {
                write!(text, "{limb}").expect("writing into a String never fails");
            } else {
                write!(text, "{limb:09}").expect("writing into a String never fails");
            }
        }
        let mut digits: Vec<u8> = text.bytes().map(|byte| byte - b'0').collect();
        let leading_zeros = digits.iter().take_while(|&&digit| digit == 0).count();
        digits.drain(..leading_zeros);
        DecimalDigits { negative, digits, exponent }
    }

    /// Writes Postgres's binary NUMERIC: ndigits, weight, sign, display scale, base-10000 digits.
    pub(crate) fn write_binary(&self, out: &mut BytesMut) -> Result<IsNull, BoxError> {
        let display_scale = (-self.exponent).max(0);
        if display_scale > NUMERIC_MAX_DISPLAY_SCALE {
            return Err(format!(
                "decimal with {display_scale} fractional digits exceeds NUMERIC's limit of {NUMERIC_MAX_DISPLAY_SCALE}"
            )
            .into());
        }
        if self.digits.is_empty() {
            out.put_i16(0);
            out.put_i16(0);
            out.put_u16(0);
            out.put_i16(display_scale as i16);
            return Ok(IsNull::No);
        }
        let point = self.digits.len() as i64 + self.exponent;
        if point > NUMERIC_MAX_INTEGER_DIGITS {
            return Err(format!("decimal with {point} integer digits overflows NUMERIC").into());
        }
        let left_padding = (4 - point.rem_euclid(4)) % 4;
        let padded_length = left_padding as usize + self.digits.len();
        let right_padding = (4 - padded_length % 4) % 4;
        let padded: Vec<u8> = std::iter::repeat_n(0, left_padding as usize)
            .chain(self.digits.iter().copied())
            .chain(std::iter::repeat_n(0, right_padding))
            .collect();
        let mut groups: Vec<i16> = padded
            .chunks(4)
            .map(|chunk| chunk.iter().fold(0i16, |group, &digit| group * 10 + i16::from(digit)))
            .collect();
        while groups.last() == Some(&0) {
            groups.pop();
        }
        let weight = (point + left_padding) / 4 - 1;
        let group_count = i16::try_from(groups.len()).map_err(|_| "decimal has too many digits for NUMERIC")?;
        let weight = i16::try_from(weight).map_err(|_| "decimal is out of NUMERIC's range")?;
        out.put_i16(group_count);
        out.put_i16(weight);
        out.put_u16(if self.negative { NUMERIC_NEGATIVE_SIGN } else { 0 });
        out.put_i16(display_scale as i16);
        for group in groups {
            out.put_i16(group);
        }
        Ok(IsNull::No)
    }
}

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
    if group_count < 0 || raw.len() != group_count as usize * 2 {
        return Err(
            format!("numeric payload declares {group_count} digit group(s) but carries {} byte(s)", raw.len()).into()
        );
    }
    let mut groups = Vec::with_capacity(group_count as usize);
    for _ in 0..group_count {
        let group = raw.get_i16();
        if !(0..10_000).contains(&group) {
            return Err(format!("invalid numeric digit group {group}").into());
        }
        groups.push(group);
    }
    let group_at = |index: i64| -> i16 {
        if index >= 0 && (index as usize) < groups.len() {
            groups[index as usize]
        } else {
            0
        }
    };
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

/// `NUMERIC`'s non-finite special values.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NonFiniteNumeric {
    NaN,
    PositiveInfinity,
    NegativeInfinity,
}

impl NonFiniteNumeric {
    /// The Postgres NUMERIC wire sign word marking each special value.
    pub(crate) const NAN_SIGN: u16 = 0xC000;
    pub(crate) const POSITIVE_INFINITY_SIGN: u16 = 0xD000;
    pub(crate) const NEGATIVE_INFINITY_SIGN: u16 = 0xF000;

    pub(crate) fn from_sign(sign: u16) -> Option<Self> {
        match sign {
            Self::NAN_SIGN => Some(Self::NaN),
            Self::POSITIVE_INFINITY_SIGN => Some(Self::PositiveInfinity),
            Self::NEGATIVE_INFINITY_SIGN => Some(Self::NegativeInfinity),
            _ => None,
        }
    }

    /// Parses `str(decimal.Decimal)` of a non-finite value; `sNaN` has no Postgres equivalent.
    pub(crate) fn from_decimal_text(text: &str) -> Option<Self> {
        match text {
            "NaN" | "-NaN" => Some(Self::NaN),
            "Infinity" => Some(Self::PositiveInfinity),
            "-Infinity" => Some(Self::NegativeInfinity),
            _ => None,
        }
    }

    pub(crate) fn from_f64(value: f64) -> Self {
        if value.is_nan() {
            Self::NaN
        } else if value > 0.0 {
            Self::PositiveInfinity
        } else {
            Self::NegativeInfinity
        }
    }

    pub(crate) fn as_text(self) -> &'static str {
        match self {
            Self::NaN => "NaN",
            Self::PositiveInfinity => "Infinity",
            Self::NegativeInfinity => "-Infinity",
        }
    }

    pub(crate) fn to_f64(self) -> f64 {
        match self {
            Self::NaN => f64::NAN,
            Self::PositiveInfinity => f64::INFINITY,
            Self::NegativeInfinity => f64::NEG_INFINITY,
        }
    }

    pub(crate) fn write_binary(self, out: &mut BytesMut) -> IsNull {
        // ndigits, weight, sign, dscale - a special value carries no digits.
        out.put_i16(0);
        out.put_i16(0);
        out.put_u16(match self {
            Self::NaN => Self::NAN_SIGN,
            Self::PositiveInfinity => Self::POSITIVE_INFINITY_SIGN,
            Self::NegativeInfinity => Self::NEGATIVE_INFINITY_SIGN,
        });
        out.put_i16(0);
        IsNull::No
    }
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
    fn numeric_binary_matches_postgres_own_layout() {
        // 12.5: ndigits 2, weight 0, sign +, dscale 1, digits [12, 5000].
        let raw = encode(&Value::Decimal("12.5".to_string()), &Type::NUMERIC).expect("must encode");
        assert_eq!(raw, vec![0, 2, 0, 0, 0, 0, 0, 1, 0, 12, 0x13, 0x88]);
    }

    #[test]
    fn numeric_rejects_values_beyond_its_own_limits() {
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
