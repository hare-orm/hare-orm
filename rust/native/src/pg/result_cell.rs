//! `ResultCell` - one value of a `ResultData`: its wire bytes, or its decoded `Value`.

use chrono::{DateTime, Utc};
use pyo3::prelude::*;
use tokio_postgres::types::Type;

use crate::pg::error::{to_pyerr, DriverError};
use crate::pg::types::numeric::decode_numeric;
use crate::pg::value::wire_scalar::WireScalar;
use crate::pg::value::{decode_value, ServerTextForms, Value};
use crate::python::ffi;

pub enum ResultCell<'a> {
    /// A value as it came off the wire - None for NULL - already checked to decode.
    Wire {
        postgres_type: &'a Type,
        raw: Option<&'a [u8]>,
    },
    Value(&'a Value),
}

impl ResultCell<'_> {
    pub fn is_null(&self) -> bool {
        match self {
            ResultCell::Wire { raw, .. } => raw.is_none(),
            ResultCell::Value(value) => matches!(value, Value::Null),
        }
    }

    /// The instant of a `timestamptz` value - None for any other value.
    pub fn get_utc_instant(&self) -> Option<DateTime<Utc>> {
        match *self {
            ResultCell::Wire { postgres_type, raw: Some(raw) } if *postgres_type == Type::TIMESTAMPTZ => {
                match WireScalar::parse(postgres_type, raw)? {
                    Ok(WireScalar::TimestampTz(instant)) => Some(instant),
                    _ => None,
                }
            }
            ResultCell::Value(Value::TimestampTz(instant)) => Some(*instant),
            _ => None,
        }
    }

    /// `read` of the cell's `Value` - decoded from the wire bytes when the cell holds them.
    pub fn with_value<R>(&self, read: impl FnOnce(&Value) -> R) -> PyResult<R> {
        match *self {
            ResultCell::Wire { raw: None, .. } => Ok(read(&Value::Null)),
            ResultCell::Wire { postgres_type, raw: Some(raw) } => {
                let value = decode_value(postgres_type, raw, &mut ServerTextForms::collecting())
                    .map_err(|error| to_pyerr(DriverError::Conversion(error.to_string())))?;
                Ok(read(&value))
            }
            ResultCell::Value(value) => Ok(read(value)),
        }
    }

    /// The UTF-8 text of a `json`/`jsonb` value as it came off the wire, without jsonb's version
    /// byte - None for any other value. The bytes were checked when the result was read.
    pub fn get_json_bytes(&self) -> Option<&[u8]> {
        match *self {
            ResultCell::Wire { postgres_type, raw: Some(raw) } if *postgres_type == Type::JSON => Some(raw),
            ResultCell::Wire { postgres_type, raw: Some(raw) } if *postgres_type == Type::JSONB => {
                match raw.split_first() {
                    Some((1, text)) => Some(text),
                    _ => None,
                }
            }
            _ => None,
        }
    }

    /// The positional text of a finite `numeric` value - None for any other value.
    pub fn get_decimal_text(&self) -> Option<String> {
        match *self {
            ResultCell::Wire { postgres_type, raw: Some(raw) } if *postgres_type == Type::NUMERIC => {
                match decode_numeric(raw) {
                    Ok(Value::Decimal(text)) => Some(text),
                    _ => None,
                }
            }
            ResultCell::Value(Value::Decimal(text)) => Some(text.clone()),
            _ => None,
        }
    }

    /// The Python object the driver gives for the value.
    pub fn to_python<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let conversion_error =
            |error: Box<dyn std::error::Error + Send + Sync>| to_pyerr(DriverError::Conversion(error.to_string()));
        if let Some(text) = self.get_json_bytes() {
            return Ok(ffi::new_string_from_utf8(py, text)?.into_any());
        }
        match *self {
            ResultCell::Wire { raw: None, .. } => Ok(py.None().into_bound(py)),
            // Checked as UTF-8 when the result was read - CPython decodes it in one pass.
            ResultCell::Wire { postgres_type, raw: Some(raw) } if WireScalar::is_text_type(postgres_type) => {
                Ok(ffi::new_string_from_utf8(py, raw)?.into_any())
            }
            ResultCell::Wire { postgres_type, raw: Some(raw) } => match WireScalar::parse(postgres_type, raw) {
                Some(scalar) => scalar.map_err(conversion_error)?.into_python(py),
                None => decode_value(postgres_type, raw, &mut ServerTextForms::collecting())
                    .map_err(conversion_error)?
                    .into_pyobject(py),
            },
            ResultCell::Value(value) => value.into_pyobject(py),
        }
    }
}
