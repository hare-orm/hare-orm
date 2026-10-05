//! `MONEY`.

use crate::pg::value::{BoxError, Value};

/// `MONEY`: an 8-byte count of the smallest currency fraction - read as a Decimal with 2
/// fractional digits, like asyncpg's money codec (the real scale is the server's `lc_monetary`).
pub(crate) fn decode_money(raw: &[u8]) -> Result<Value, BoxError> {
    let bytes: [u8; 8] = raw.try_into().map_err(|_| format!("invalid money payload length {}", raw.len()))?;
    let cents = i128::from(i64::from_be_bytes(bytes));
    let sign = if cents < 0 { "-" } else { "" };
    Ok(Value::Decimal(format!("{sign}{}.{:02}", cents.abs() / 100, cents.abs() % 100)))
}

#[cfg(test)]
mod tests {

    use tokio_postgres::types::Type;

    use crate::pg::value::test_support::*;
    use crate::pg::value::Value;

    #[test]
    fn money_decodes_as_decimal_with_two_fractional_digits() {
        assert!(matches!(decode(&Type::MONEY, &12345i64.to_be_bytes()), Value::Decimal(text) if text == "123.45"));
        assert!(matches!(decode(&Type::MONEY, &(-5i64).to_be_bytes()), Value::Decimal(text) if text == "-0.05"));
    }
}
