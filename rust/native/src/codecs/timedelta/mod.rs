//! A `TimeDeltaField`, stored as a BIGINT of microseconds.

mod timedelta_read;
mod timedelta_write;

pub use timedelta_read::TimeDeltaRead;
pub use timedelta_write::TimeDeltaWrite;

const MICROSECONDS_PER_SECOND: i64 = 1_000_000;

const SECONDS_PER_DAY: i64 = 86_400;
