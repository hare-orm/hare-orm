//! `WireScalar` - a value of a common built-in type read from its wire bytes without an allocation.
//! A result's values are checked with it on the tokio side and made Python objects from it on the
//! event loop's thread; `decode_value` reads these types through it as well.

use chrono::{DateTime, NaiveDate, NaiveDateTime, Utc};
use pyo3::prelude::*;
use pyo3::types::PyString;
use tokio_postgres::types::{FromSql, Type};

use crate::pg::types::datetime::{decode_date, decode_timestamp};
use crate::pg::types::json::decode_json_text;
use crate::pg::value::{BoxError, Value};
use crate::python::objects::new_uuid;

pub enum WireScalar<'a> {
    Bool(bool),
    Int(i64),
    Float(f64),
    /// Text - and a `json`/`jsonb` document's text.
    Text(&'a str),
    Uuid(uuid::Uuid),
    Timestamp(NaiveDateTime),
    TimestampTz(DateTime<Utc>),
    Date(NaiveDate),
}

impl<'a> WireScalar<'a> {
    /// Whether a value of type `ty` is text whose binary form is the text itself - text, varchar,
    /// bpchar, name, xml, cstring, refcursor, unknown.
    pub fn is_text_type(postgres_type: &Type) -> bool {
        matches!(postgres_type.oid(), 25 | 1043 | 1042 | 19 | 142 | 2275 | 1790 | 705)
    }

    /// The value of `raw`, the wire bytes of a non-NULL value of type `ty` - None for a type read
    /// another way.
    pub fn parse(postgres_type: &Type, raw: &'a [u8]) -> Option<Result<Self, BoxError>> {
        if Self::is_text_type(postgres_type) {
            return Some(std::str::from_utf8(raw).map(WireScalar::Text).map_err(BoxError::from));
        }
        Some(match postgres_type.oid() {
            16 => bool::from_sql(postgres_type, raw).map(WireScalar::Bool),
            21 => i16::from_sql(postgres_type, raw).map(|value| WireScalar::Int(value.into())),
            23 => i32::from_sql(postgres_type, raw).map(|value| WireScalar::Int(value.into())),
            20 => i64::from_sql(postgres_type, raw).map(WireScalar::Int),
            700 => f32::from_sql(postgres_type, raw).map(|value| WireScalar::Float(value.into())),
            701 => f64::from_sql(postgres_type, raw).map(WireScalar::Float),
            114 | 3802 => decode_json_text(postgres_type, raw).map(WireScalar::Text),
            2950 => uuid::Uuid::from_slice(raw).map(WireScalar::Uuid).map_err(BoxError::from),
            1114 => decode_timestamp(raw).map(WireScalar::Timestamp),
            1184 => decode_timestamp(raw).map(|value| WireScalar::TimestampTz(value.and_utc())),
            1082 => decode_date(raw).map(WireScalar::Date),
            _ => return None,
        })
    }

    pub fn into_value(self) -> Value {
        match self {
            WireScalar::Bool(value) => Value::Bool(value),
            WireScalar::Int(value) => Value::Int(value),
            WireScalar::Float(value) => Value::Float(value),
            WireScalar::Text(text) => Value::Text(text.to_string()),
            WireScalar::Uuid(value) => Value::Uuid(value),
            WireScalar::Timestamp(value) => Value::Timestamp(value),
            WireScalar::TimestampTz(value) => Value::TimestampTz(value),
            WireScalar::Date(value) => Value::Date(value),
        }
    }

    /// The Python object of the value - the one its `Value` gives.
    pub fn into_python(self, py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
        Ok(match self {
            WireScalar::Bool(value) => value.into_pyobject(py)?.to_owned().into_any(),
            WireScalar::Int(value) => value.into_pyobject(py)?.into_any(),
            WireScalar::Float(value) => value.into_pyobject(py)?.into_any(),
            WireScalar::Text(text) => PyString::new(py, text).into_any(),
            WireScalar::Uuid(value) => new_uuid(py, value.as_u128())?,
            WireScalar::Timestamp(value) => value.into_pyobject(py)?.into_any(),
            WireScalar::TimestampTz(value) => value.into_pyobject(py)?.into_any(),
            WireScalar::Date(value) => value.into_pyobject(py)?.into_any(),
        })
    }
}
