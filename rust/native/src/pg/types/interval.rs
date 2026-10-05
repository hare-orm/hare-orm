//! `INTERVAL` - months, days and microseconds kept apart.

use bytes::Buf;

use crate::pg::value::{BoxError, IntervalValue};

/// asyncpg's own interval -> timedelta conversion: a year counts as 365 days, a month as 30.
pub(crate) const DAYS_PER_INTERVAL_YEAR: i64 = 365;
pub(crate) const DAYS_PER_INTERVAL_MONTH: i64 = 30;

/// `INTERVAL`: 8-byte microseconds, 4-byte days, 4-byte months.
pub(crate) fn decode_interval(mut raw: &[u8]) -> Result<IntervalValue, BoxError> {
    if raw.len() != 16 {
        return Err(format!("invalid interval payload length {}", raw.len()).into());
    }
    let microseconds = raw.get_i64();
    let days = raw.get_i32();
    let months = raw.get_i32();
    Ok(IntervalValue { months, days, microseconds })
}

#[cfg(test)]
mod tests {

    use tokio_postgres::types::Type;

    use crate::pg::value::test_support::*;
    use crate::pg::value::{IntervalValue, Value};

    #[test]
    fn interval_keeps_months_days_and_microseconds_apart() {
        let mut raw = Vec::new();
        raw.extend_from_slice(&3_723_000_001i64.to_be_bytes());
        raw.extend_from_slice(&5i32.to_be_bytes());
        raw.extend_from_slice(&14i32.to_be_bytes());
        assert!(matches!(
            decode(&Type::INTERVAL, &raw),
            Value::Interval(IntervalValue { months: 14, days: 5, microseconds: 3_723_000_001 })
        ));
    }
    #[test]
    fn interval_binds_months_days_and_microseconds() {
        let interval = Value::Interval(IntervalValue { months: 0, days: -1, microseconds: 7_200_000_000 });
        let raw = encode(&interval, &Type::INTERVAL).unwrap();
        assert!(matches!(
            decode(&Type::INTERVAL, &raw),
            Value::Interval(IntervalValue { months: 0, days: -1, microseconds: 7_200_000_000 })
        ));
        assert!(encode(&interval, &Type::INT8).is_err());
    }
}
