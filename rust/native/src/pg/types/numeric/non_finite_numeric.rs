use bytes::{BufMut, BytesMut};
use tokio_postgres::types::IsNull;

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
