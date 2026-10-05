//! The parts of the byte keys the SQLite sort key functions build: a SQLite value's storage class
//! first, as SQLite orders values of different classes, then the class's own key.

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyFloat, PyInt, PyString};

use crate::sqlite_functions::decimal_number::DecimalNumber;

/// Key prefix of an INTEGER or REAL value - after NULL, before TEXT.
pub const NUMBER_CLASS: u8 = 0x10;
/// Key prefix of a TEXT value.
pub const TEXT_CLASS: u8 = 0x20;
/// Key prefix of a BLOB value - after every other class.
pub const BLOB_CLASS: u8 = 0x30;

/// A SQLite value passed to a function.
pub enum SqliteValue<'a> {
    Null,
    Number(DecimalNumber),
    Text(&'a str),
    Blob(&'a [u8]),
}

impl<'a> SqliteValue<'a> {
    /// The value of a function argument; None for a number this reads no key of (an infinite
    /// REAL, an integer past 128 bits).
    pub fn read(value: &'a Bound<'_, PyAny>) -> PyResult<Option<Self>> {
        if value.is_none() {
            return Ok(Some(SqliteValue::Null));
        }
        if let Ok(text) = value.cast::<PyString>() {
            return Ok(Some(SqliteValue::Text(text.to_str()?)));
        }
        if let Ok(blob) = value.cast::<PyBytes>() {
            return Ok(Some(SqliteValue::Blob(blob.as_bytes())));
        }
        if value.is_instance_of::<PyInt>() {
            let Ok(number) = value.extract::<i128>() else {
                return Ok(None);
            };
            let digits = number.unsigned_abs().to_string().into_bytes();
            return Ok(Some(SqliteValue::Number(DecimalNumber::from_digits(number < 0, digits, 0))));
        }
        if let Ok(number) = value.cast::<PyFloat>() {
            let number = number.value();
            if !number.is_finite() {
                return Ok(None);
            }
            let mut buffer = ryu::Buffer::new();
            return Ok(DecimalNumber::parse(buffer.format_finite(number)).map(SqliteValue::Number));
        }
        Ok(None)
    }
}

/// Writes `text` so that keys compare as the texts do (by code point) and no key is the start of
/// another: each 0x00 byte escaped as 0x00 0xFF, the end marked by 0x00 0x00.
pub fn write_terminated_text(text: &str, key: &mut Vec<u8>) {
    for &byte in text.as_bytes() {
        key.push(byte);
        if byte == 0 {
            key.push(0xFF);
        }
    }
    key.extend_from_slice(&[0x00, 0x00]);
}
