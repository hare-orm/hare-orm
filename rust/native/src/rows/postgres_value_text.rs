//! The text PostgreSQL writes of a binary `tsvector` or `hstore` value - what asyncpg's codecs of
//! these types give, read in binary so that asyncpg's text array parser never sees them.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use tokio_postgres::types::{FromSql, Type};

use crate::pg::decode_context::PgDecodeContext;
use crate::pg::types::text_search_vector::PgTsVectorText;
use crate::pg::value::BoxError;
use crate::pg::wire_reader::PgWireReader;

/// The text `tsvectorout` writes of a binary `tsvector`.
#[pyfunction]
pub fn read_tsvector_text(value: &[u8]) -> PyResult<String> {
    PgTsVectorText::from_sql(&Type::TS_VECTOR, value)
        .map(|text| text.0)
        .map_err(|error| PyValueError::new_err(error.to_string()))
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
    decode_hstore_text(value).map_err(|error| PyValueError::new_err(error.to_string()))
}

fn decode_hstore_text(value: &[u8]) -> Result<String, BoxError> {
    let mut context = PgDecodeContext::new(value.len());
    let mut reader = PgWireReader::new(value, "hstore");
    let count = reader.count(8)?;
    context.reserve_values(count)?;
    let mut output = String::with_capacity(value.len());
    for index in 0..count {
        if index > 0 {
            output.push_str(", ");
        }
        let key = reader.nullable_bytes()?.ok_or("hstore key is NULL")?;
        push_quoted(&mut output, std::str::from_utf8(key)?);
        output.push_str("=>");
        match reader.nullable_bytes()? {
            None => output.push_str("NULL"),
            Some(item) => push_quoted(&mut output, std::str::from_utf8(item)?),
        }
    }
    reader.finish()?;
    context.reserve_bytes(output.len())?;
    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hstore_rejects_impossible_counts_lengths_and_trailing_bytes() {
        assert!(decode_hstore_text(&(-1_i32).to_be_bytes()).is_err());
        assert!(decode_hstore_text(&i32::MAX.to_be_bytes()).is_err());
        assert!(decode_hstore_text(&[0, 0, 0, 0, 1]).is_err());
        let mut raw = 1_i32.to_be_bytes().to_vec();
        raw.extend_from_slice(&0_i32.to_be_bytes());
        raw.extend_from_slice(&(-2_i32).to_be_bytes());
        assert!(decode_hstore_text(&raw).is_err());
    }

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
