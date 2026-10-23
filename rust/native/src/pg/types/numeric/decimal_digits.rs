use crate::pg::value::BoxError;
use bytes::{BufMut, BytesMut};
use tokio_postgres::types::IsNull;

use super::*;

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
        let exponent_bits = ((bits >> 52) & 0x7FF).cast_signed();
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
