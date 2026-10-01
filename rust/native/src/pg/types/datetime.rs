//! Timestamps, dates and times of day, and the infinities Python reads as its extremes.

use chrono::{NaiveDate, NaiveDateTime, NaiveTime, Timelike};
use tokio_postgres::types::{FromSql, Type};

use crate::pg::value::BoxError;

/// Microseconds in one day - the exclusive upper bound of a Python `datetime.time`.
pub(crate) const MICROSECONDS_PER_DAY: i64 = 86_400_000_000;

/// Postgres's `infinity`/`-infinity` timestamp and date wire values, and the Python values they
/// decode to - `datetime.max`/`datetime.min` and `date.max`/`date.min`, as asyncpg returns them.
pub(crate) const TIMESTAMP_POSITIVE_INFINITY: i64 = i64::MAX;
pub(crate) const TIMESTAMP_NEGATIVE_INFINITY: i64 = i64::MIN;
pub(crate) const DATE_POSITIVE_INFINITY: i32 = i32::MAX;
pub(crate) const DATE_NEGATIVE_INFINITY: i32 = i32::MIN;

pub(crate) fn python_max_date() -> NaiveDate {
    NaiveDate::from_ymd_opt(9999, 12, 31).expect("9999-12-31 is a valid date")
}

pub(crate) fn python_min_date() -> NaiveDate {
    NaiveDate::from_ymd_opt(1, 1, 1).expect("0001-01-01 is a valid date")
}

pub(crate) fn python_max_datetime() -> NaiveDateTime {
    python_max_date().and_hms_micro_opt(23, 59, 59, 999_999).expect("23:59:59.999999 is a valid time")
}

pub(crate) fn python_min_datetime() -> NaiveDateTime {
    python_min_date().and_hms_opt(0, 0, 0).expect("00:00:00 is a valid time")
}

/// The `infinity`/`-infinity` wire value a timestamp stands for, if any.
pub(crate) fn infinite_timestamp_wire_value(value: &NaiveDateTime) -> Option<i64> {
    if *value == python_max_datetime() {
        Some(TIMESTAMP_POSITIVE_INFINITY)
    } else if *value == python_min_datetime() {
        Some(TIMESTAMP_NEGATIVE_INFINITY)
    } else {
        None
    }
}

/// The `infinity`/`-infinity` wire value a date stands for, if any.
pub(crate) fn infinite_date_wire_value(value: NaiveDate) -> Option<i32> {
    if value == python_max_date() {
        Some(DATE_POSITIVE_INFINITY)
    } else if value == python_min_date() {
        Some(DATE_NEGATIVE_INFINITY)
    } else {
        None
    }
}

/// The infinity a timestamp payload encodes, if any - `Some(true)` for `infinity`.
pub(crate) fn timestamp_infinity(raw: &[u8]) -> Option<bool> {
    let bytes: [u8; 8] = raw.try_into().ok()?;
    match i64::from_be_bytes(bytes) {
        TIMESTAMP_POSITIVE_INFINITY => Some(true),
        TIMESTAMP_NEGATIVE_INFINITY => Some(false),
        _ => None,
    }
}

pub(crate) fn decode_timestamp(raw: &[u8]) -> Result<NaiveDateTime, BoxError> {
    Ok(match timestamp_infinity(raw) {
        Some(true) => python_max_datetime(),
        Some(false) => python_min_datetime(),
        None => NaiveDateTime::from_sql(&Type::TIMESTAMP, raw)?,
    })
}

pub(crate) fn decode_date(raw: &[u8]) -> Result<NaiveDate, BoxError> {
    Ok(match <[u8; 4]>::try_from(raw).ok().map(i32::from_be_bytes) {
        Some(DATE_POSITIVE_INFINITY) => python_max_date(),
        Some(DATE_NEGATIVE_INFINITY) => python_min_date(),
        _ => NaiveDate::from_sql(&Type::DATE, raw)?,
    })
}

/// A time of day from its microseconds since midnight - `24:00:00`, which Postgres allows and
/// Python's `datetime.time` cannot hold, is an error rather than a silent `00:00:00`.
pub(crate) fn time_of_day(microseconds: i64) -> Result<NaiveTime, BoxError> {
    if !(0..MICROSECONDS_PER_DAY).contains(&microseconds) {
        let seconds = microseconds.div_euclid(1_000_000);
        return Err(format!(
            "time value {:02}:{:02}:{:02}.{:06} is out of range for Python's datetime.time",
            seconds / 3600,
            seconds / 60 % 60,
            seconds % 60,
            microseconds.rem_euclid(1_000_000)
        )
        .into());
    }
    let seconds = (microseconds / 1_000_000) as u32;
    let nanoseconds = (microseconds % 1_000_000) as u32 * 1000;
    NaiveTime::from_num_seconds_from_midnight_opt(seconds, nanoseconds).ok_or_else(|| "invalid time of day".into())
}

pub(crate) fn time_microseconds(value: NaiveTime) -> i64 {
    i64::from(value.num_seconds_from_midnight()) * 1_000_000 + i64::from(value.nanosecond() / 1000)
}

#[cfg(test)]
mod tests {
    use super::*;
    use bytes::BytesMut;
    use chrono::NaiveTime;
    use tokio_postgres::types::{ToSql, Type};

    use crate::pg::value::decode::{decode_value, ServerTextForms};
    use crate::pg::value::test_support::*;
    use crate::pg::value::Value;

    #[test]
    fn infinite_python_extremes_encode_back_to_infinity() {
        let cases: [(Value, Type, Vec<u8>); 6] = [
            (Value::Date(python_max_date()), Type::DATE, i32::MAX.to_be_bytes().to_vec()),
            (Value::Date(python_min_date()), Type::DATE, i32::MIN.to_be_bytes().to_vec()),
            (Value::Timestamp(python_max_datetime()), Type::TIMESTAMP, i64::MAX.to_be_bytes().to_vec()),
            (Value::Timestamp(python_min_datetime()), Type::TIMESTAMPTZ, i64::MIN.to_be_bytes().to_vec()),
            (Value::TimestampTz(python_max_datetime().and_utc()), Type::TIMESTAMPTZ, i64::MAX.to_be_bytes().to_vec()),
            (Value::TimestampTz(python_min_datetime().and_utc()), Type::TIMESTAMPTZ, i64::MIN.to_be_bytes().to_vec()),
        ];
        for (value, ty, expected) in cases {
            let mut out = BytesMut::new();
            value.to_sql(&ty, &mut out).expect("must encode");
            assert_eq!(out.to_vec(), expected, "{value:?} as {}", ty.name());
        }
    }
    #[test]
    fn infinite_timestamps_and_dates_decode_to_the_python_extremes() {
        let positive = i64::MAX.to_be_bytes();
        let negative = i64::MIN.to_be_bytes();
        match decode(&Type::TIMESTAMPTZ, &positive) {
            Value::TimestampTz(v) => assert_eq!(v.naive_utc(), python_max_datetime()),
            other => panic!("unexpected {other:?}"),
        }
        match decode(&Type::TIMESTAMP, &negative) {
            Value::Timestamp(v) => assert_eq!(v, python_min_datetime()),
            other => panic!("unexpected {other:?}"),
        }
        match decode(&Type::DATE, &i32::MAX.to_be_bytes()) {
            Value::Date(v) => assert_eq!(v, python_max_date()),
            other => panic!("unexpected {other:?}"),
        }
    }
    #[test]
    fn time_of_day_24_00_is_an_error_not_midnight() {
        assert!(
            decode_value(&Type::TIME, &MICROSECONDS_PER_DAY.to_be_bytes(), &mut ServerTextForms::collecting()).is_err()
        );
        let mut timetz = MICROSECONDS_PER_DAY.to_be_bytes().to_vec();
        timetz.extend_from_slice(&0i32.to_be_bytes());
        assert!(decode_value(&Type::TIMETZ, &timetz, &mut ServerTextForms::collecting()).is_err());
        let noon = 43_200_000_000i64.to_be_bytes();
        assert!(
            matches!(decode(&Type::TIME, &noon), Value::Time(time) if time == NaiveTime::from_hms_opt(12, 0, 0).unwrap())
        );
    }
}
