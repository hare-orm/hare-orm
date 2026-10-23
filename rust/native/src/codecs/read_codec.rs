//! How a driver value becomes an attribute value.

use pyo3::prelude::*;
use pyo3::types::PyType;
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::options::Options;
use crate::codecs::{array, binary, boolean, date, datetime, decimal, enumeration, json, range, time, timedelta, uuid};
use crate::pg::result_cell::ResultCell;
use crate::python::references::PythonReferences;

pub enum ReadCodec {
    /// The value as the driver returns it.
    AsIs,
    /// `field_type(value)` unless None or already of that type.
    FieldType(Py<PyAny>),
    /// The value as it is when exactly of `exact_type`, else the reader - for a column whose
    /// driver may return another type (an expression of the field).
    ExactType {
        exact_type: Py<PyType>,
        reader: Py<PyAny>,
    },
    Boolean(boolean::BooleanRead),
    Binary(binary::BinaryRead),
    Datetime(datetime::DatetimeRead),
    Date(date::DateRead),
    Time(time::TimeRead),
    TimeDelta(timedelta::TimeDeltaRead),
    Uuid(uuid::UuidRead),
    Enumeration(enumeration::EnumerationRead),
    Decimal(decimal::DecimalRead),
    Json(json::JsonRead),
    Array(array::ArrayRead),
    Range(range::RangeRead),
    /// The field's own reader for every value.
    Call(Py<PyAny>),
}

impl ReadCodec {
    /// The codec `type` built with `options`.
    pub fn build(codec_type: &str, options: &Options<'_>) -> PyResult<Self> {
        Ok(match codec_type {
            "as_is" => ReadCodec::AsIs,
            "field_type" => ReadCodec::FieldType(options.get_object("field_type")?),
            "exact_type" => ReadCodec::ExactType {
                exact_type: options.get_object("exact_type")?.into_bound(options.py()).cast_into::<PyType>()?.unbind(),
                reader: options.get_object("reader")?,
            },
            "boolean" => ReadCodec::Boolean(boolean::BooleanRead::from_options(options)?),
            "binary" => ReadCodec::Binary(binary::BinaryRead::from_options(options)?),
            "datetime" => ReadCodec::Datetime(datetime::DatetimeRead::from_options(options)?),
            "date" => ReadCodec::Date(date::DateRead::from_options(options)?),
            "time" => ReadCodec::Time(time::TimeRead::from_options(options)?),
            "timedelta" => ReadCodec::TimeDelta(timedelta::TimeDeltaRead::from_options(options)?),
            "uuid" => ReadCodec::Uuid(uuid::UuidRead::from_options(options)?),
            "enumeration" => ReadCodec::Enumeration(enumeration::EnumerationRead::from_options(options)?),
            "decimal" => ReadCodec::Decimal(decimal::DecimalRead::from_options(options)?),
            "json" => ReadCodec::Json(json::JsonRead::from_options(options)?),
            "array" => ReadCodec::Array(array::ArrayRead::from_options(options)?),
            "range" => ReadCodec::Range(range::RangeRead::from_options(options)?),
            "call" => ReadCodec::Call(options.get_object("reader")?),
            other => return Err(options.error(&format!("unknown read codec {other:?}"))),
        })
    }

    /// The attribute value of `raw`, a value the driver returned.
    pub fn read<'py>(&self, raw: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = raw.py();
        match self {
            ReadCodec::AsIs => Ok(raw),
            ReadCodec::FieldType(field_type) => {
                let field_type = field_type.bind(py);
                if raw.is_none() || raw.is_instance(field_type)? {
                    Ok(raw)
                } else {
                    field_type.call1((raw,))
                }
            }
            ReadCodec::ExactType { exact_type, reader } => {
                if raw.get_type().is(exact_type.bind(py)) {
                    Ok(raw)
                } else {
                    reader.bind(py).call1((raw,))
                }
            }
            ReadCodec::Boolean(codec) => codec.read(raw),
            ReadCodec::Binary(codec) => codec.read(raw),
            ReadCodec::Datetime(codec) => codec.read(raw),
            ReadCodec::Date(codec) => codec.read(raw),
            ReadCodec::Time(codec) => codec.read(raw),
            ReadCodec::TimeDelta(codec) => codec.read(raw),
            ReadCodec::Uuid(codec) => codec.read(raw),
            ReadCodec::Enumeration(codec) => codec.read(raw),
            ReadCodec::Decimal(codec) => codec.read(raw),
            ReadCodec::Json(codec) => codec.read(raw),
            ReadCodec::Array(codec) => codec.read(raw),
            ReadCodec::Range(codec) => codec.read(raw),
            ReadCodec::Call(reader) => reader.bind(py).call1((raw,)),
        }
    }

    /// The attribute value of `cell`, a value of a `rust.native.pg` result - what `read()` gives for
    /// the object the driver would have returned for it.
    pub fn read_cell<'py>(&self, py: Python<'py>, cell: &ResultCell<'_>) -> PyResult<Bound<'py, PyAny>> {
        match self {
            ReadCodec::AsIs => cell.to_python(py),
            ReadCodec::Datetime(codec) => {
                match cell.get_utc_instant().and_then(|instant| codec.read_utc_instant(py, instant)) {
                    Some(value) => value,
                    None => codec.read(cell.to_python(py)?),
                }
            }
            ReadCodec::Json(codec) => match cell.get_json_bytes() {
                Some(text) => codec.read_text(py, text),
                None => codec.read(cell.to_python(py)?),
            },
            ReadCodec::Array(codec) => codec.read_cell(py, cell),
            ReadCodec::Range(codec) => codec.read_cell(py, cell),
            ReadCodec::Decimal(codec) => match cell.get_decimal_text() {
                Some(text) => codec.read_text(py, &text),
                None => codec.read(cell.to_python(py)?),
            },
            _ => self.read(cell.to_python(py)?),
        }
    }
}

impl PythonReferences for ReadCodec {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        match self {
            ReadCodec::AsIs => Ok(()),
            ReadCodec::FieldType(field_type) => visit.call(field_type),
            ReadCodec::ExactType { exact_type, reader } => {
                visit.call(exact_type)?;
                visit.call(reader)
            }
            ReadCodec::Boolean(codec) => codec.traverse(visit),
            ReadCodec::Binary(codec) => codec.traverse(visit),
            ReadCodec::Datetime(codec) => codec.traverse(visit),
            ReadCodec::Date(codec) => codec.traverse(visit),
            ReadCodec::Time(codec) => codec.traverse(visit),
            ReadCodec::TimeDelta(codec) => codec.traverse(visit),
            ReadCodec::Uuid(codec) => codec.traverse(visit),
            ReadCodec::Enumeration(codec) => codec.traverse(visit),
            ReadCodec::Decimal(codec) => codec.traverse(visit),
            ReadCodec::Json(codec) => codec.traverse(visit),
            ReadCodec::Array(codec) => codec.traverse(visit),
            ReadCodec::Range(codec) => codec.traverse(visit),
            ReadCodec::Call(reader) => visit.call(reader),
        }
    }
}
