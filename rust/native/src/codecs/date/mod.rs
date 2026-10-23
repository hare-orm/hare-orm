//! A `DateField`: a date as it is, a datetime narrowed to its date, SQLite's ISO text parsed.

mod date_read;
mod date_write;

pub use date_read::DateRead;
pub use date_write::DateWrite;
