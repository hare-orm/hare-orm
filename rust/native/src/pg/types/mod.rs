//! Wire formats of the PostgreSQL types `Value` encodes and decodes, one file per type.

pub mod array;
pub mod bit_string;
pub mod composite;
pub mod datetime;
pub mod interval;
pub mod json;
pub mod mac_address;
pub mod money;
pub mod network;
pub mod numeric;
pub mod range;
pub mod text_search_query;
pub mod text_search_vector;
pub mod time_with_zone;
pub mod vector;

use bytes::Buf;

use crate::pg::value::BoxError;

/// Splits one length-prefixed value off `raw` - `None` for a `-1` (NULL) length.
pub(crate) fn read_length_prefixed<'a>(raw: &mut &'a [u8], what: &str) -> Result<Option<&'a [u8]>, BoxError> {
    let len = raw.try_get_i32().map_err(|error| -> BoxError { format!("malformed {what} length: {error}").into() })?;
    if len == -1 {
        return Ok(None);
    }
    let len = usize::try_from(len).map_err(|_| -> BoxError { format!("negative {what} length {len}").into() })?;
    if raw.len() < len {
        return Err(format!("{what} length {len} exceeds {} remaining byte(s) in the payload", raw.len()).into());
    }
    let (value_bytes, rest) = raw.split_at(len);
    *raw = rest;
    Ok(Some(value_bytes))
}
