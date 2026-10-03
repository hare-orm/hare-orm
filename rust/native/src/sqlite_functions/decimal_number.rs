//! An exact decimal number read from its text or a double, its order-preserving byte key, and the
//! number rounded to a count of fractional digits as `Decimal.quantize()` rounds it.

use std::fmt::Write;

/// A decimal number - its sign, significant digits and exponent, as `decimal.Decimal` holds it.
#[derive(Debug, PartialEq, Eq)]
pub enum DecimalNumber {
    Zero,
    Finite {
        negative: bool,
        /// ASCII digits, the first and the last not zero.
        digits: Vec<u8>,
        /// The decimal exponent of the first digit.
        exponent: i64,
    },
}

impl DecimalNumber {
    /// The number of a text in the plain form `[+-]digits[.digits][e[+-]digits]` (a side of the
    /// point may be empty); None for any other text - one `Decimal()` reads otherwise, or not at all.
    pub fn parse(text: &str) -> Option<Self> {
        let bytes = text.as_bytes();
        let mut position = 0;
        let negative = match bytes.first() {
            Some(b'-') => {
                position = 1;
                true
            }
            Some(b'+') => {
                position = 1;
                false
            }
            _ => false,
        };
        let mut digits = Vec::new();
        let mut fraction_length: i64 = 0;
        let mut seen_point = false;
        let mut seen_digit = false;
        while let Some(&byte) = bytes.get(position) {
            if byte.is_ascii_digit() {
                digits.push(byte);
                seen_digit = true;
                if seen_point {
                    fraction_length += 1;
                }
            } else if byte == b'.' && !seen_point {
                seen_point = true;
            } else {
                break;
            }
            position += 1;
        }
        if !seen_digit {
            return None;
        }
        let mut exponent: i64 = 0;
        if let Some(&marker) = bytes.get(position) {
            if marker != b'e' && marker != b'E' {
                return None;
            }
            position += 1;
            let exponent_text = &text[position..];
            let unsigned = exponent_text.strip_prefix(['+', '-']).unwrap_or(exponent_text);
            if unsigned.is_empty() || !unsigned.bytes().all(|byte| byte.is_ascii_digit()) || unsigned.len() > 15 {
                return None;
            }
            exponent = exponent_text.parse().ok()?;
        }
        Some(Self::from_digits(negative, digits, exponent - fraction_length))
    }

    /// The number `[-]digits * 10**exponent`, `digits` ASCII.
    pub fn from_digits(negative: bool, mut digits: Vec<u8>, mut exponent: i64) -> Self {
        let leading_zeros = digits.iter().take_while(|&&digit| digit == b'0').count();
        digits.drain(..leading_zeros);
        while digits.last() == Some(&b'0') {
            digits.pop();
            exponent += 1;
        }
        if digits.is_empty() {
            return DecimalNumber::Zero;
        }
        let first_exponent = exponent + digits.len() as i64 - 1;
        DecimalNumber::Finite { negative, digits, exponent: first_exponent }
    }

    /// Writes the number's byte key: one number's key sorts before another's exactly when it is
    /// smaller, equal numbers have equal keys, and no key is the start of another.
    pub fn write_key(&self, key: &mut Vec<u8>) {
        match self {
            DecimalNumber::Zero => key.push(0x80),
            DecimalNumber::Finite { negative: false, digits, exponent } => {
                key.push(0x81);
                key.extend_from_slice(&biased(*exponent).to_be_bytes());
                key.extend_from_slice(digits);
                key.push(0x00);
            }
            DecimalNumber::Finite { negative: true, digits, exponent } => {
                // Every byte inverted orders the larger magnitude first; 0xFF ends the digits
                // above any inverted digit, so a shorter prefix sorts later.
                key.push(0x7F);
                key.extend((!biased(*exponent)).to_be_bytes());
                key.extend(digits.iter().map(|digit| !digit));
                key.push(0xFF);
            }
        }
    }
}

impl DecimalNumber {
    /// The exact value of a finite double, as `Decimal(float)` holds it - every binary digit
    /// written out in decimal.
    pub fn from_float(number: f64) -> Self {
        let bits = number.to_bits();
        let negative = bits >> 63 == 1;
        let biased_exponent = i64::try_from((bits >> 52) & 0x7FF).expect("an 11-bit exponent");
        let fraction = bits & ((1 << 52) - 1);
        let (mantissa, binary_exponent) =
            if biased_exponent == 0 { (fraction, -1074) } else { (fraction | (1 << 52), biased_exponent - 1075) };
        let mut limbs = LargeNumber::new(mantissa);
        let decimal_exponent = if binary_exponent >= 0 {
            limbs.multiply_by_power(2, binary_exponent);
            0
        } else {
            // m * 2**-k = m * 5**k * 10**-k
            limbs.multiply_by_power(5, -binary_exponent);
            binary_exponent
        };
        Self::from_digits(negative, limbs.get_digits(), decimal_exponent)
    }

    /// `format(value.normalize(), "f")` - the digits in positional notation, no trailing fractional
    /// zeros.
    pub fn format_positional(&self) -> String {
        let DecimalNumber::Finite { negative, digits, exponent } = self else {
            return "0".to_owned();
        };
        let digits = std::str::from_utf8(digits).expect("ASCII digits");
        let mut text = String::from(if *negative { "-" } else { "" });
        if *exponent < 0 {
            text.push_str("0.");
            text.extend(std::iter::repeat_n('0', usize::try_from(-exponent - 1).expect("a positive count")));
            text.push_str(digits);
        } else {
            let whole_length = usize::try_from(exponent + 1).expect("a positive count");
            if digits.len() <= whole_length {
                text.push_str(digits);
                text.extend(std::iter::repeat_n('0', whole_length - digits.len()));
            } else {
                text.push_str(&digits[..whole_length]);
                text.push('.');
                text.push_str(&digits[whole_length..]);
            }
        }
        text
    }

    /// The number rounded to `scale` fractional digits, as `quantize(Decimal(10) ** -scale)` rounds
    /// it; None when the result would have more than `precision` digits, where `quantize()` raises.
    pub fn round_to_scale(&self, scale: i64, rounding: Rounding, precision: usize) -> Option<ScaledNumber> {
        let DecimalNumber::Finite { negative, digits, exponent } = self else {
            return Some(ScaledNumber { negative: false, coefficient: Vec::new(), scale });
        };
        let length = i64::try_from(digits.len()).ok()?;
        // The power of ten of the last digit, counted in units of the scale.
        let shift = exponent.checked_sub(length - 1)?.checked_add(scale)?;
        let mut coefficient = if shift >= 0 {
            let coefficient_length = usize::try_from(length.checked_add(shift)?).ok()?;
            if coefficient_length > precision {
                return None;
            }
            let mut coefficient = digits.clone();
            coefficient.resize(coefficient_length, b'0');
            coefficient
        } else {
            let dropped = usize::try_from(-shift).ok()?;
            let kept_length = digits.len().saturating_sub(dropped);
            let mut kept = digits[..kept_length].to_vec();
            // Past every digit the first dropped digit is a leading zero of the number.
            let first_dropped = if dropped <= digits.len() { digits[kept_length] } else { b'0' };
            let has_more = dropped <= digits.len() && kept_length + 1 < digits.len();
            let rounds_up = match rounding {
                Rounding::HalfUp => first_dropped >= b'5',
                Rounding::HalfEven => {
                    let is_odd = kept.last().is_some_and(|digit| (digit - b'0') % 2 == 1);
                    first_dropped > b'5' || (first_dropped == b'5' && (has_more || is_odd))
                }
            };
            if rounds_up {
                increment(&mut kept);
            }
            kept
        };
        while coefficient.first() == Some(&b'0') {
            coefficient.remove(0);
        }
        if coefficient.len() > precision {
            return None;
        }
        Some(ScaledNumber { negative: *negative && !coefficient.is_empty(), coefficient, scale })
    }
}

/// Adds one to ASCII digits, a carry past the first one adding a digit.
fn increment(digits: &mut Vec<u8>) {
    for digit in digits.iter_mut().rev() {
        if *digit == b'9' {
            *digit = b'0';
        } else {
            *digit += 1;
            return;
        }
    }
    digits.insert(0, b'1');
}

/// How a number rounds to fewer digits.
#[derive(Clone, Copy)]
pub enum Rounding {
    /// A tie away from zero, `ROUND_HALF_UP`.
    HalfUp,
    /// A tie to the even digit, `ROUND_HALF_EVEN`.
    HalfEven,
}

/// A number with a fixed count of fractional digits: `[-]coefficient * 10**-scale`.
pub struct ScaledNumber {
    /// Never set for zero - a numeric column has no negative zero.
    pub negative: bool,
    /// ASCII digits without leading zeros, empty for zero.
    pub coefficient: Vec<u8>,
    pub scale: i64,
}

impl ScaledNumber {
    /// `Decimal.adjusted()` - the power of ten of the first digit, `-scale` for zero.
    pub fn get_adjusted_exponent(&self) -> i64 {
        i64::try_from(self.coefficient.len().max(1)).expect("a short coefficient") - 1 - self.scale
    }

    /// `format(value, "f")` of a non-negative scale - every fractional digit written.
    pub fn format_fixed(&self) -> String {
        let coefficient = if self.coefficient.is_empty() {
            "0"
        } else {
            std::str::from_utf8(&self.coefficient).expect("ASCII digits")
        };
        let scale = usize::try_from(self.scale).expect("a non-negative scale");
        let mut text = String::from(if self.negative { "-" } else { "" });
        if scale == 0 {
            text.push_str(coefficient);
        } else if coefficient.len() <= scale {
            text.push_str("0.");
            text.extend(std::iter::repeat_n('0', scale - coefficient.len()));
            text.push_str(coefficient);
        } else {
            let whole_length = coefficient.len() - scale;
            let _ = write!(text, "{}.{}", &coefficient[..whole_length], &coefficient[whole_length..]);
        }
        text
    }
}

/// A non-negative integer of any size, in base 10**9 limbs, the lowest first.
struct LargeNumber {
    limbs: Vec<u32>,
}

impl LargeNumber {
    const BASE: u64 = 1_000_000_000;

    fn new(value: u64) -> Self {
        let mut limbs = Vec::new();
        let mut rest = value;
        while rest > 0 {
            limbs.push(u32::try_from(rest % Self::BASE).expect("a limb below the base"));
            rest /= Self::BASE;
        }
        LargeNumber { limbs }
    }

    fn multiply(&mut self, factor: u32) {
        let mut carry = 0_u64;
        for limb in &mut self.limbs {
            let product = u64::from(*limb) * u64::from(factor) + carry;
            *limb = u32::try_from(product % Self::BASE).expect("a limb below the base");
            carry = product / Self::BASE;
        }
        while carry > 0 {
            self.limbs.push(u32::try_from(carry % Self::BASE).expect("a limb below the base"));
            carry /= Self::BASE;
        }
    }

    /// Multiplies by `base ** power`, `base` 2 or 5, in factors that fit a limb.
    fn multiply_by_power(&mut self, base: u32, power: i64) {
        let largest_power: u32 = if base == 2 { 29 } else { 13 };
        let mut rest = u32::try_from(power).expect("a double's exponent");
        while rest > 0 {
            let step = rest.min(largest_power);
            self.multiply(base.pow(step));
            rest -= step;
        }
    }

    /// The ASCII digits, empty for zero.
    fn get_digits(&self) -> Vec<u8> {
        let mut text = String::new();
        for (index, limb) in self.limbs.iter().rev().enumerate() {
            if index == 0 {
                let _ = write!(text, "{limb}");
            } else {
                let _ = write!(text, "{limb:09}");
            }
        }
        text.into_bytes()
    }
}

/// `value` as an unsigned number in the same order - the sign bit flipped.
pub fn biased(value: i64) -> u64 {
    (value as u64) ^ (1 << 63)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(text: &str) -> Vec<u8> {
        let mut key = Vec::new();
        DecimalNumber::parse(text).expect("a plain number").write_key(&mut key);
        key
    }

    #[test]
    fn reads_plain_forms() {
        assert_eq!(DecimalNumber::parse("0.00"), Some(DecimalNumber::Zero));
        assert_eq!(DecimalNumber::parse("-0"), Some(DecimalNumber::Zero));
        assert_eq!(
            DecimalNumber::parse("120.50"),
            Some(DecimalNumber::Finite { negative: false, digits: b"1205".to_vec(), exponent: 2 })
        );
        assert_eq!(
            DecimalNumber::parse("-.5e-3"),
            Some(DecimalNumber::Finite { negative: true, digits: b"5".to_vec(), exponent: -4 })
        );
        for text in ["", "-", ".", "1e", "1e+", "1.2.3", " 1", "1 ", "NaN", "inf", "1_000", "0x10"] {
            assert_eq!(DecimalNumber::parse(text), None, "{text}");
        }
    }

    #[test]
    fn keys_sort_as_the_numbers() {
        let ordered = [
            "-1e20", "-123.45", "-123.4", "-12", "-1.5", "-1", "-0.001", "0", "0.001", "1", "1.5", "12", "123.4",
            "123.45", "1e20",
        ];
        for pair in ordered.windows(2) {
            assert!(key(pair[0]) < key(pair[1]), "{} < {}", pair[0], pair[1]);
        }
        assert_eq!(key("1.50"), key("1.5"));
        assert_eq!(key("15e-1"), key("1.5"));
    }

    fn round(text: &str, scale: i64, rounding: Rounding) -> Option<String> {
        DecimalNumber::parse(text)
            .expect("a plain number")
            .round_to_scale(scale, rounding, 28)
            .map(|n| n.format_fixed())
    }

    #[test]
    fn rounds_to_a_scale() {
        assert_eq!(round("1.005", 2, Rounding::HalfUp).as_deref(), Some("1.01"));
        assert_eq!(round("1.005", 2, Rounding::HalfEven).as_deref(), Some("1.00"));
        assert_eq!(round("1.0051", 2, Rounding::HalfEven).as_deref(), Some("1.01"));
        assert_eq!(round("-0.004", 2, Rounding::HalfUp).as_deref(), Some("0.00"));
        assert_eq!(round("9.999", 2, Rounding::HalfUp).as_deref(), Some("10.00"));
        assert_eq!(round("0.0005", 3, Rounding::HalfUp).as_deref(), Some("0.001"));
        assert_eq!(round("0.00005", 3, Rounding::HalfUp).as_deref(), Some("0.000"));
        assert_eq!(round("12", 0, Rounding::HalfEven).as_deref(), Some("12"));
        assert_eq!(round("1e30", 2, Rounding::HalfEven), None);
    }

    #[test]
    fn reads_doubles_exactly() {
        assert_eq!(DecimalNumber::from_float(0.5), DecimalNumber::parse("0.5").expect("a number"));
        assert_eq!(
            DecimalNumber::from_float(0.1),
            DecimalNumber::parse("0.1000000000000000055511151231257827021181583404541015625").expect("a number")
        );
        assert_eq!(DecimalNumber::from_float(-1e20), DecimalNumber::parse("-1e20").expect("a number"));
        assert_eq!(DecimalNumber::from_float(0.0), DecimalNumber::Zero);
    }
}
