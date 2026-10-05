//! `TIMETZ`.

use bytes::Buf;
use chrono::{FixedOffset, NaiveTime};
use tokio_postgres::types::{FromSql, Type};

use crate::pg::types::datetime::time_of_day;
use crate::pg::value::BoxError;

/// `TIMETZ`: 8-byte microseconds since midnight, then a 4-byte zone in seconds WEST of UTC.
pub(crate) struct PgTimeTzCell(pub(crate) NaiveTime, pub(crate) FixedOffset);

impl<'a> FromSql<'a> for PgTimeTzCell {
    fn from_sql(_ty: &Type, mut raw: &'a [u8]) -> Result<Self, BoxError> {
        if raw.len() != 12 {
            return Err(format!("invalid timetz payload length {}", raw.len()).into());
        }
        let time = time_of_day(raw.get_i64())?;
        let zone_west = raw.get_i32();
        let offset = FixedOffset::east_opt(-zone_west).ok_or("timetz zone offset out of range")?;
        Ok(PgTimeTzCell(time, offset))
    }

    fn accepts(postgres_type: &Type) -> bool {
        *postgres_type == Type::TIMETZ
    }
}
