use std::fmt::Write;

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
