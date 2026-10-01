//! How an attribute value becomes a bound value.

use pyo3::prelude::*;
use pyo3::types::PyString;
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::options::Options;
use crate::codecs::{date, datetime, decimal, enumeration, json, scalar, time, timedelta, uuid};
use crate::python::references::PythonReferences;

pub enum WriteCodec {
    Scalar(scalar::ScalarWrite),
    Datetime(datetime::DatetimeWrite),
    Date(date::DateWrite),
    Time(time::TimeWrite),
    TimeDelta(timedelta::TimeDeltaWrite),
    Uuid(uuid::UuidWrite),
    Enumeration(enumeration::EnumerationWrite),
    Decimal(decimal::DecimalWrite),
    Json(json::JsonWrite),
    /// The field's own writer, `(value, instance) -> value`, for every value.
    Call(Py<PyAny>),
}

impl WriteCodec {
    /// The codec `kind` built with `options`.
    pub fn build(kind: &str, options: &Options<'_>) -> PyResult<Self> {
        Ok(match kind {
            "scalar" => WriteCodec::Scalar(scalar::ScalarWrite::from_options(options)?),
            "datetime" => WriteCodec::Datetime(datetime::DatetimeWrite::from_options(options)?),
            "date" => WriteCodec::Date(date::DateWrite::from_options(options)?),
            "time" => WriteCodec::Time(time::TimeWrite::from_options(options)?),
            "timedelta" => WriteCodec::TimeDelta(timedelta::TimeDeltaWrite::from_options(options)?),
            "uuid" => WriteCodec::Uuid(uuid::UuidWrite::from_options(options)?),
            "enumeration" => WriteCodec::Enumeration(enumeration::EnumerationWrite::from_options(options)?),
            "decimal" => WriteCodec::Decimal(decimal::DecimalWrite::from_options(options)?),
            "json" => WriteCodec::Json(json::JsonWrite::from_options(options)?),
            "call" => WriteCodec::Call(options.get_object("writer")?),
            other => return Err(options.error(&format!("unknown write codec {other:?}"))),
        })
    }

    /// The value bound for `value`, an attribute value of `instance` (a model instance, the model,
    /// or None) held under `field_name`.
    pub fn write<'py>(
        &self,
        value: Bound<'py, PyAny>,
        instance: &Bound<'py, PyAny>,
        field_name: &Bound<'py, PyString>,
    ) -> PyResult<Bound<'py, PyAny>> {
        match self {
            WriteCodec::Scalar(codec) => codec.write(value, instance, field_name),
            WriteCodec::Datetime(codec) => codec.write(value, instance, field_name),
            WriteCodec::Date(codec) => codec.write(value, instance),
            WriteCodec::Time(codec) => codec.write(value, instance),
            WriteCodec::TimeDelta(codec) => codec.write(value, instance),
            WriteCodec::Uuid(codec) => codec.write(value, instance),
            WriteCodec::Enumeration(codec) => codec.write(value, instance),
            WriteCodec::Decimal(codec) => codec.write(value, instance, field_name),
            WriteCodec::Json(codec) => codec.write(value, instance),
            WriteCodec::Call(writer) => writer.bind(value.py()).call1((value, instance)),
        }
    }
}

impl PythonReferences for WriteCodec {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        match self {
            WriteCodec::Scalar(codec) => codec.traverse(visit),
            WriteCodec::Datetime(codec) => codec.traverse(visit),
            WriteCodec::Date(codec) => codec.traverse(visit),
            WriteCodec::Time(codec) => codec.traverse(visit),
            WriteCodec::TimeDelta(codec) => codec.traverse(visit),
            WriteCodec::Uuid(codec) => codec.traverse(visit),
            WriteCodec::Enumeration(codec) => codec.traverse(visit),
            WriteCodec::Decimal(codec) => codec.traverse(visit),
            WriteCodec::Json(codec) => codec.traverse(visit),
            WriteCodec::Call(writer) => visit.call(writer),
        }
    }
}
