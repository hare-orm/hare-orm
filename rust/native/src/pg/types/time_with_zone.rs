//! `TIMETZ`.

use crate::pg::types::datetime::time_of_day;
use crate::pg::value::{BoxError, Value};
use crate::pg::wire_reader::PgWireReader;
use chrono::FixedOffset;

/// `TIMETZ`: 8-byte microseconds since midnight, then a 4-byte zone in seconds WEST of UTC.
pub(crate) fn decode_time_with_zone(raw: &[u8]) -> Result<Value, BoxError> {
    if raw.len() != 12 {
        return Err(format!("invalid timetz payload length {}", raw.len()).into());
    }
    let mut reader = PgWireReader::new(raw, "timetz");
    let time = time_of_day(reader.i64()?)?;
    let zone_west = reader.i32()?;
    reader.finish()?;
    let offset = FixedOffset::east_opt(zone_west.checked_neg().ok_or("timetz zone offset out of range")?)
        .ok_or("timetz zone offset out of range")?;
    Ok(Value::TimeTz(time, offset))
}
