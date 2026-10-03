//! `MACADDR`/`MACADDR8`.

use crate::pg::value::{BoxError, Value};

/// `MACADDR` (6 bytes) / `MACADDR8` (8 bytes) as colon-separated lowercase hex.
pub(crate) fn decode_macaddr(raw: &[u8]) -> Result<Value, BoxError> {
    if raw.len() != 6 && raw.len() != 8 {
        return Err(format!("invalid macaddr payload length {}", raw.len()).into());
    }
    Ok(Value::Text(raw.iter().map(|byte| format!("{byte:02x}")).collect::<Vec<_>>().join(":")))
}

#[cfg(test)]
mod tests {
    use super::*;

    use tokio_postgres::types::Type;

    use crate::pg::value::test_support::*;
    use crate::pg::value::Value;

    #[test]
    fn macaddr_renders_colon_separated_lowercase_hex() {
        assert!(
            matches!(decode(&Type::MACADDR, &[0x08, 0x00, 0x2b, 0x01, 0x02, 0x03]), Value::Text(text) if text == "08:00:2b:01:02:03")
        );
        assert!(matches!(
            decode(&Type::MACADDR8, &[0x08, 0x00, 0x2b, 0x01, 0x02, 0x03, 0x04, 0x05]),
            Value::Text(text) if text == "08:00:2b:01:02:03:04:05"
        ));
    }

    #[test]
    fn macaddr_rejects_wrong_length_payload() {
        let raw: &[u8] = &[0x08, 0x00, 0x2b];
        assert!(decode_macaddr(raw).is_err());
    }
}
