//! `Value` - a database value between Python objects and the PostgreSQL wire format. Its base (the
//! scalar variants and their Python conversions) is adapted from yara-orm's `rust/src/value.rs`
//! (<https://github.com/vsdudakov/yara-orm>, MIT License); the `Array`, `Range`, `Interval`, `Network`
//! and `Composite` values and the wire encoding and decoding are hare-orm's own.
//!
//! Every value is encoded and decoded here, checked against the parameter or column type the server
//! resolved - a value is never written as another type's raw bytes.

pub mod decode;
pub mod encode;
pub mod from_python;
pub mod into_python;
pub mod python_classes;
#[cfg(test)]
pub(crate) mod test_support;
#[cfg(test)]
mod tests;

pub(crate) use decode::{
    decode_pg_row_values, type_may_need_server_text_form, ServerTextForms, SERVER_TEXT_FORMS_PER_QUERY,
};
pub(crate) use encode::{BinaryParameter, RawWireValue};

use std::error::Error;
use std::net::IpAddr;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, Utc};
use tokio_postgres::types::{Kind as TypeShape, Type};

use crate::pg::types::numeric::NonFiniteNumeric;

pub(crate) type BoxError = Box<dyn Error + Sync + Send>;

/// Returns `ty`'s underlying base type when it is a domain (through any number of levels).
pub(crate) fn domain_base_type(ty: &Type) -> &Type {
    let mut current = ty;
    while let TypeShape::Domain(base) = current.kind() {
        current = base;
    }
    current
}

#[derive(Debug, Clone)]
pub struct RangeValue {
    pub lower: Option<Box<Value>>,
    pub upper: Option<Box<Value>>,
    pub lower_inc: bool,
    pub upper_inc: bool,
    pub empty: bool,
}

/// An `INTERVAL` - Postgres keeps months, days and microseconds apart.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IntervalValue {
    pub months: i32,
    pub days: i32,
    pub microseconds: i64,
}

/// An `INET`/`CIDR` value.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NetworkValue {
    pub address: IpAddr,
    pub prefix_length: u8,
    pub is_cidr: bool,
}

#[derive(Debug, Clone)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    /// A Python `int` outside the `i64` range, as its decimal text.
    BigInt(String),
    Float(f64),
    Text(String),
    Bytes(Vec<u8>),
    /// A JSON document's text (a Python dict bound as a parameter).
    Json(String),
    Array(Vec<Value>),
    Range(RangeValue),
    Uuid(uuid::Uuid),
    /// A finite `NUMERIC` as decimal text - `str(decimal.Decimal)` when bound, the text
    /// `numeric_out` prints when read.
    Decimal(String),
    /// `NUMERIC`'s `NaN`/`Infinity`/`-Infinity`.
    NonFiniteDecimal(NonFiniteNumeric),
    Timestamp(NaiveDateTime),
    TimestampTz(DateTime<Utc>),
    Date(NaiveDate),
    Time(NaiveTime),
    /// `TIMETZ` - a time-of-day plus a fixed UTC offset.
    TimeTz(NaiveTime, FixedOffset),
    /// `INTERVAL` - a Python `timedelta`.
    Interval(IntervalValue),
    /// `INET`/`CIDR` - a Python `ipaddress` object.
    Network(NetworkValue),
    /// A composite (`ROW(...)`) value - a Python tuple.
    Composite(Vec<Value>),
}
