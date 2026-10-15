//! The text PostgreSQL writes of a binary `tsvector` or `hstore` value - what asyncpg's codecs of
//! these types give, read in binary so that asyncpg's text array parser never sees them.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use tokio_postgres::types::{FromSql, Type};

use crate::pg::types::text_search_vector::PgTsVectorText;

/// The text `tsvectorout` writes of a binary `tsvector`.
#[pyfunction]
pub fn read_tsvector_text(value: &[u8]) -> PyResult<String> {
    PgTsVectorText::from_sql(&Type::TS_VECTOR, value)
        .map(|text| text.0)
        .map_err(|error| PyValueError::new_err(error.to_string()))
}

/// Takes a 4-byte big-endian length off the front of `raw`.
fn take_length(raw: &mut &[u8]) -> PyResult<i32> {
    let (length, rest) = raw
        .split_first_chunk::<4>()
        .ok_or_else(|| PyValueError::new_err("invalid hstore payload: truncated length"))?;
    *raw = rest;
    Ok(i32::from_be_bytes(*length))
}

/// Takes `length` bytes of UTF-8 text off the front of `raw`.
fn take_text<'a>(raw: &mut &'a [u8], length: i32) -> PyResult<&'a str> {
    let length =
        usize::try_from(length).map_err(|_| PyValueError::new_err("invalid hstore payload: negative length"))?;
    if raw.len() < length {
        return Err(PyValueError::new_err("invalid hstore payload: truncated text"));
    }
    let (text, rest) = raw.split_at(length);
    *raw = rest;
    std::str::from_utf8(text).map_err(|error| PyValueError::new_err(format!("invalid hstore payload: {error}")))
}

/// Writes `text` double-quoted, as `hstore_out` does - a `"` or `\` escaped by a backslash.
fn push_quoted(output: &mut String, text: &str) {
    output.push('"');
    let mut start = 0;
    for (index, byte) in text.bytes().enumerate() {
        if byte == b'"' || byte == b'\\' {
            output.push_str(&text[start..index]);
            output.push('\\');
            start = index;
        }
    }
    output.push_str(&text[start..]);
    output.push('"');
}

/// The text `hstore_out` writes of a binary `hstore`: `"key"=>"value", "other"=>NULL`.
#[pyfunction]
pub fn read_hstore_text(value: &[u8]) -> PyResult<String> {
    let mut raw = value;
    let count = take_length(&mut raw)?;
    let mut output = String::with_capacity(value.len() + 8 * usize::try_from(count).unwrap_or(0));
    for index in 0..count {
        if index > 0 {
            output.push_str(", ");
        }
        let key_length = take_length(&mut raw)?;
        push_quoted(&mut output, take_text(&mut raw, key_length)?);
        output.push_str("=>");
        let item_length = take_length(&mut raw)?;
        if item_length < 0 {
            output.push_str("NULL");
        } else {
            push_quoted(&mut output, take_text(&mut raw, item_length)?);
        }
    }
    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pair(key: &str, item: Option<&str>) -> Vec<u8> {
        let mut raw = Vec::new();
        raw.extend_from_slice(&i32::try_from(key.len()).unwrap().to_be_bytes());
        raw.extend_from_slice(key.as_bytes());
        match item {
            Some(item) => {
                raw.extend_from_slice(&i32::try_from(item.len()).unwrap().to_be_bytes());
                raw.extend_from_slice(item.as_bytes());
            }
            None => raw.extend_from_slice(&(-1i32).to_be_bytes()),
        }
        raw
    }

    #[test]
    fn hstore_text_quotes_and_escapes_like_hstore_out() {
        let mut raw = 2i32.to_be_bytes().to_vec();
        raw.extend(pair("a\"b", Some("c\\d")));
        raw.extend(pair("n", None));
        assert_eq!(read_hstore_text(&raw).unwrap(), "\"a\\\"b\"=>\"c\\\\d\", \"n\"=>NULL");
    }

    #[test]
    fn empty_hstore_is_empty_text() {
        assert_eq!(read_hstore_text(&0i32.to_be_bytes()).unwrap(), "");
    }

    #[test]
    fn truncated_hstore_is_refused() {
        let mut raw = 1i32.to_be_bytes().to_vec();
        raw.extend_from_slice(&5i32.to_be_bytes());
        raw.extend_from_slice(b"ab");
        assert!(read_hstore_text(&raw).is_err());
    }
}
