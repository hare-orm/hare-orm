//! `Value` -> wire bytes of a bind parameter, checked against the type the server resolved.

use std::fmt::Write as _;

use bytes::{BufMut, BytesMut};
use chrono::{FixedOffset, Local, NaiveDateTime, NaiveTime, TimeZone, Timelike, Utc};
use tokio_postgres::types::{to_sql_checked, Format, IsNull, Kind as TypeShape, ToSql, Type};

use crate::pg::types::array::{
    encode_array, is_text_format_array, member_type_is_text_compatible, write_text_array_literal,
};
use crate::pg::types::datetime::{infinite_date_wire_value, infinite_timestamp_wire_value, time_microseconds};
use crate::pg::types::json::{write_json_float, write_json_string};
use crate::pg::types::numeric::{DecimalDigits, NonFiniteNumeric};
use crate::pg::types::range::{encode_multirange, encode_range};
use crate::pg::value::{domain_base_type, BoxError, Value};

impl Value {
    /// The PostgreSQL type to declare for this value as a bind parameter, so the server uses it
    /// instead of inferring from context. `None` lets the server infer it: a NULL has no type;
    /// a string may bind to any column type (a `JSONField` value arrives pre-encoded as a string, a
    /// UUID/inet/geography string is parsed by the server); an int/decimal-text/array/range/JSON
    /// value takes its real type from the target column or cast; a naive datetime must know
    /// whether it lands in TIMESTAMP or TIMESTAMPTZ (interpreted as the client's local time for the
    /// latter, like asyncpg). Fixing INT8 for ints would break overloads such as
    /// `array_length(anyarray, int4)`. A float is FLOAT8 - hare has no float4 column type.
    pub fn pg_type(&self) -> Option<Type> {
        Some(match self {
            Value::Bool(_) => Type::BOOL,
            Value::Bytes(_) => Type::BYTEA,
            Value::Uuid(_) => Type::UUID,
            Value::Decimal(_) | Value::NonFiniteDecimal(_) => Type::NUMERIC,
            Value::Float(_) => Type::FLOAT8,
            Value::TimestampTz(_) => Type::TIMESTAMPTZ,
            Value::Date(_) => Type::DATE,
            Value::Time(_) => Type::TIME,
            Value::TimeTz(..) => Type::TIMETZ,
            Value::Interval(_) => Type::INTERVAL,
            Value::Timestamp(_)
            | Value::Null
            | Value::Text(_)
            | Value::Json(_)
            | Value::Array(_)
            | Value::Range(_)
            | Value::Int(_)
            | Value::BigInt(_)
            | Value::Network(_)
            | Value::Composite(_) => return None,
        })
    }

    /// The value's text form, used when the server resolved a text-family parameter type.
    pub(crate) fn as_pg_text(&self) -> Option<String> {
        match self {
            Value::Bool(value) => Some(if *value { "true" } else { "false" }.to_string()),
            Value::Int(value) => Some(value.to_string()),
            Value::BigInt(value) | Value::Decimal(value) | Value::Json(value) => Some(value.clone()),
            Value::Float(value) => Some(value.to_string()),
            Value::Uuid(value) => Some(value.to_string()),
            Value::NonFiniteDecimal(value) => Some(value.as_text().to_string()),
            Value::Timestamp(value) => Some(value.format("%Y-%m-%d %H:%M:%S%.6f").to_string()),
            Value::TimestampTz(value) => Some(value.to_rfc3339()),
            Value::Date(value) => Some(value.format("%Y-%m-%d").to_string()),
            Value::Time(value) => Some(value.format("%H:%M:%S%.6f").to_string()),
            Value::TimeTz(value, offset) => Some(format!("{}{}", value.format("%H:%M:%S%.6f"), offset)),
            Value::Null
            | Value::Text(_)
            | Value::Bytes(_)
            | Value::Array(_)
            | Value::Range(_)
            | Value::Interval(_)
            | Value::Network(_)
            | Value::Composite(_) => None,
        }
    }

    /// A short name of the value's type for error messages.
    pub(crate) fn variant_type_name(&self) -> &'static str {
        match self {
            Value::Null => "NULL",
            Value::Bool(_) => "bool",
            Value::Int(_) | Value::BigInt(_) => "int",
            Value::Float(_) => "float",
            Value::Text(_) => "string",
            Value::Bytes(_) => "bytes",
            Value::Json(_) => "JSON",
            Value::Array(_) => "list",
            Value::Range(_) => "range",
            Value::Uuid(_) => "UUID",
            Value::Decimal(_) | Value::NonFiniteDecimal(_) => "decimal",
            Value::Timestamp(_) => "naive datetime",
            Value::TimestampTz(_) => "aware datetime",
            Value::Date(_) => "date",
            Value::Time(_) | Value::TimeTz(..) => "time",
            Value::Interval(_) => "timedelta",
            Value::Network(_) => "network address",
            Value::Composite(_) => "composite",
        }
    }

    pub(crate) fn wrong_type(&self, postgres_type: &Type) -> BoxError {
        format!("cannot bind a {} value to a parameter of type {}", self.variant_type_name(), postgres_type.name())
            .into()
    }

    /// Appends the JSON form of a value bound as an element of a list sent to a json/jsonb
    /// parameter - mirrors how a Python value nested in a dict is serialized.
    pub(crate) fn write_json(&self, out: &mut String) -> Result<(), String> {
        match self {
            Value::Null => out.push_str("null"),
            Value::Bool(value) => out.push_str(if *value { "true" } else { "false" }),
            Value::Int(value) => write!(out, "{value}").expect("writing into a String never fails"),
            Value::BigInt(value) | Value::Json(value) => out.push_str(value),
            Value::Float(value) => write_json_float(*value, out)?,
            Value::Text(value) | Value::Decimal(value) => write_json_string(value, out),
            Value::Array(items) => {
                out.push('[');
                for (index, item) in items.iter().enumerate() {
                    if index > 0 {
                        out.push(',');
                    }
                    item.write_json(out)?;
                }
                out.push(']');
            }
            Value::Uuid(value) => write_json_string(&value.to_string(), out),
            Value::NonFiniteDecimal(value) => write_json_string(value.as_text(), out),
            Value::Timestamp(value) => write_json_string(&iso_datetime_text(value), out),
            Value::TimestampTz(value) => {
                write_json_string(&format!("{}+00:00", iso_datetime_text(&value.naive_utc())), out);
            }
            Value::Date(value) => write_json_string(&value.format("%Y-%m-%d").to_string(), out),
            Value::Time(value) => write_json_string(&iso_time_text(*value), out),
            Value::TimeTz(value, offset) => write_json_string(&format!("{}{}", iso_time_text(*value), offset), out),
            Value::Bytes(_) | Value::Range(_) | Value::Interval(_) | Value::Network(_) | Value::Composite(_) => {
                return Err(format!("a {} value is not JSON serialisable", self.variant_type_name()));
            }
        }
        Ok(())
    }

    /// Encodes the value as a parameter of type `ty`, checking that the two match.
    /// `top_level` is true for a bind parameter whose wire format `encode_format` negotiated, and
    /// false wherever the binary format is required (array elements, range bounds, COPY).
    pub(crate) fn encode(&self, postgres_type: &Type, out: &mut BytesMut, top_level: bool) -> Result<IsNull, BoxError> {
        let postgres_type = domain_base_type(postgres_type);
        if matches!(*postgres_type, Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::NAME | Type::UNKNOWN) {
            if let Some(text) = self.as_pg_text() {
                out.extend_from_slice(text.as_bytes());
                return Ok(IsNull::No);
            }
        }
        match self {
            Value::Null => Ok(IsNull::Yes),
            Value::Bool(value) => self.encode_bool(*value, postgres_type, out, top_level),
            Value::Int(value) => {
                encode_integer(*value, postgres_type, out).ok_or_else(|| self.wrong_type(postgres_type))?
            }
            Value::BigInt(text) => self.encode_big_integer(text, postgres_type, out),
            Value::Float(value) => self.encode_float(*value, postgres_type, out),
            Value::Text(value) => Self::encode_text(value, postgres_type, out, top_level),
            Value::Bytes(value) => match *postgres_type {
                Type::BYTEA => {
                    out.extend_from_slice(value);
                    Ok(IsNull::No)
                }
                _ => Err(self.wrong_type(postgres_type)),
            },
            Value::Json(text) => self.encode_json(text, postgres_type, out),
            Value::Array(items) => self.encode_list(items, postgres_type, out, top_level),
            Value::Range(range) => encode_range(range, postgres_type, out),
            Value::Uuid(value) => match *postgres_type {
                Type::UUID => {
                    out.extend_from_slice(value.as_bytes());
                    Ok(IsNull::No)
                }
                _ => Err(self.wrong_type(postgres_type)),
            },
            Value::Decimal(text) => self.encode_decimal_text(text, postgres_type, out),
            Value::NonFiniteDecimal(value) => Self::encode_non_finite_decimal(*value, postgres_type, out),
            Value::Timestamp(value) => self.encode_timestamp(value, postgres_type, out, top_level),
            Value::TimestampTz(value) => match *postgres_type {
                Type::TIMESTAMPTZ => match infinite_timestamp_wire_value(&value.naive_utc()) {
                    Some(infinity) => {
                        out.put_i64(infinity);
                        Ok(IsNull::No)
                    }
                    None => value.to_sql(postgres_type, out),
                },
                _ => Err(self.wrong_type(postgres_type)),
            },
            Value::Date(value) => match *postgres_type {
                Type::DATE => match infinite_date_wire_value(*value) {
                    Some(infinity) => {
                        out.put_i32(infinity);
                        Ok(IsNull::No)
                    }
                    None => value.to_sql(postgres_type, out),
                },
                _ => Err(self.wrong_type(postgres_type)),
            },
            Value::Time(value) => self.encode_time(*value, postgres_type, out, top_level),
            Value::TimeTz(value, offset) => match *postgres_type {
                Type::TIMETZ => {
                    out.put_i64(time_microseconds(*value));
                    // The wire's zone field is seconds WEST of UTC.
                    out.put_i32(-offset.local_minus_utc());
                    Ok(IsNull::No)
                }
                _ => Err(self.wrong_type(postgres_type)),
            },
            Value::Interval(interval) => match *postgres_type {
                Type::INTERVAL => {
                    out.put_i64(interval.microseconds);
                    out.put_i32(interval.days);
                    out.put_i32(interval.months);
                    Ok(IsNull::No)
                }
                _ => Err(self.wrong_type(postgres_type)),
            },
            Value::Network(_) | Value::Composite(_) => Err(self.wrong_type(postgres_type)),
        }
    }

    fn encode_bool(
        &self,
        value: bool,
        postgres_type: &Type,
        out: &mut BytesMut,
        top_level: bool,
    ) -> Result<IsNull, BoxError> {
        match *postgres_type {
            Type::BOOL => {
                out.put_u8(u8::from(value));
                Ok(IsNull::No)
            }
            Type::INT2 | Type::INT4 | Type::INT8 | Type::NUMERIC | Type::FLOAT4 | Type::FLOAT8 => {
                Value::Int(i64::from(value)).encode(postgres_type, out, top_level)
            }
            _ => Err(self.wrong_type(postgres_type)),
        }
    }

    /// An integer past `i64`, kept as its text.
    fn encode_big_integer(&self, text: &str, postgres_type: &Type, out: &mut BytesMut) -> Result<IsNull, BoxError> {
        match *postgres_type {
            Type::NUMERIC | Type::FLOAT4 | Type::FLOAT8 => self.encode_decimal_text(text, postgres_type, out),
            _ if encode_integer(0, postgres_type, &mut BytesMut::new()).is_some() => {
                Err(format!("integer {text} is out of range for type {}", postgres_type.name()).into())
            }
            _ => Err(self.wrong_type(postgres_type)),
        }
    }

    fn encode_float(&self, value: f64, postgres_type: &Type, out: &mut BytesMut) -> Result<IsNull, BoxError> {
        match *postgres_type {
            Type::FLOAT4 => {
                out.put_f32(value as f32);
                Ok(IsNull::No)
            }
            Type::FLOAT8 => {
                out.put_f64(value);
                Ok(IsNull::No)
            }
            Type::NUMERIC if value.is_finite() => DecimalDigits::from_f64(value).write_binary(out),
            Type::NUMERIC => Ok(NonFiniteNumeric::from_f64(value).write_binary(out)),
            _ => Err(self.wrong_type(postgres_type)),
        }
    }

    /// A finite decimal number written as text.
    fn encode_decimal_text(&self, text: &str, postgres_type: &Type, out: &mut BytesMut) -> Result<IsNull, BoxError> {
        match *postgres_type {
            Type::NUMERIC => DecimalDigits::parse(text)?.write_binary(out),
            Type::FLOAT4 => {
                out.put_f32(text.parse::<f32>()?);
                Ok(IsNull::No)
            }
            Type::FLOAT8 => {
                out.put_f64(text.parse::<f64>()?);
                Ok(IsNull::No)
            }
            _ => Err(self.wrong_type(postgres_type)),
        }
    }

    fn encode_non_finite_decimal(
        value: NonFiniteNumeric,
        postgres_type: &Type,
        out: &mut BytesMut,
    ) -> Result<IsNull, BoxError> {
        match *postgres_type {
            Type::NUMERIC => Ok(value.write_binary(out)),
            Type::FLOAT4 => {
                out.put_f32(value.to_f64() as f32);
                Ok(IsNull::No)
            }
            Type::FLOAT8 => {
                out.put_f64(value.to_f64());
                Ok(IsNull::No)
            }
            _ => {
                Err(format!("cannot bind decimal {} to a parameter of type {}", value.as_text(), postgres_type.name())
                    .into())
            }
        }
    }

    fn encode_text(text: &str, postgres_type: &Type, out: &mut BytesMut, top_level: bool) -> Result<IsNull, BoxError> {
        match *postgres_type {
            Type::UUID => {
                out.extend_from_slice(uuid::Uuid::parse_str(text)?.as_bytes());
                Ok(IsNull::No)
            }
            // A pre-encoded JSON string is sent as-is - json's binary format is its text,
            // jsonb's is a version byte followed by the text. The server validates it.
            Type::JSON => {
                out.extend_from_slice(text.as_bytes());
                Ok(IsNull::No)
            }
            Type::JSONB => {
                out.put_u8(1);
                out.extend_from_slice(text.as_bytes());
                Ok(IsNull::No)
            }
            // At the top level `encode_format` declared the text format, so the server parses
            // the string with the type's own input function.
            _ if top_level || member_type_is_text_compatible(postgres_type) => {
                out.extend_from_slice(text.as_bytes());
                Ok(IsNull::No)
            }
            _ => Err(format!(
                "cannot bind a string where a binary {} value is required (an array element, a range bound \
                 or a COPY column); pass a value of the column's native type instead",
                postgres_type.name()
            )
            .into()),
        }
    }

    fn encode_json(&self, text: &str, postgres_type: &Type, out: &mut BytesMut) -> Result<IsNull, BoxError> {
        match *postgres_type {
            Type::JSON => {
                out.extend_from_slice(text.as_bytes());
                Ok(IsNull::No)
            }
            Type::JSONB => {
                out.put_u8(1);
                out.extend_from_slice(text.as_bytes());
                Ok(IsNull::No)
            }
            _ => Err(self.wrong_type(postgres_type)),
        }
    }

    fn encode_list(
        &self,
        items: &[Value],
        postgres_type: &Type,
        out: &mut BytesMut,
        top_level: bool,
    ) -> Result<IsNull, BoxError> {
        // A list bound to a json/jsonb parameter is a JSON array, not a Postgres array.
        if matches!(*postgres_type, Type::JSON | Type::JSONB) {
            let mut text = String::new();
            self.write_json(&mut text)?;
            return Value::Json(text).encode(postgres_type, out, top_level);
        }
        if matches!(postgres_type.kind(), TypeShape::Multirange(_)) {
            return encode_multirange(items, postgres_type, out);
        }
        if !matches!(postgres_type.kind(), TypeShape::Array(_)) {
            return Err(format!("cannot bind a list to a parameter of non-array type {}", postgres_type.name()).into());
        }
        if top_level && is_text_format_array(self, postgres_type) {
            let mut literal = String::new();
            write_text_array_literal(items, &mut literal);
            out.extend_from_slice(literal.as_bytes());
            return Ok(IsNull::No);
        }
        encode_array(items, postgres_type, out)
    }

    fn encode_timestamp(
        &self,
        value: &NaiveDateTime,
        postgres_type: &Type,
        out: &mut BytesMut,
        top_level: bool,
    ) -> Result<IsNull, BoxError> {
        match *postgres_type {
            Type::TIMESTAMP => match infinite_timestamp_wire_value(value) {
                Some(infinity) => {
                    out.put_i64(infinity);
                    Ok(IsNull::No)
                }
                None => value.to_sql(postgres_type, out),
            },
            Type::TIMESTAMPTZ => {
                if let Some(infinity) = infinite_timestamp_wire_value(value) {
                    out.put_i64(infinity);
                    return Ok(IsNull::No);
                }
                // Interpreted as the client machine's local wall-clock time, like asyncpg.
                let aware = Local
                    .from_local_datetime(value)
                    .single()
                    .ok_or("ambiguous or nonexistent local datetime (DST transition)")?;
                aware.with_timezone(&Utc).to_sql(postgres_type, out)
            }
            // A naive datetime bound to a date keeps its date, as asyncpg does.
            Type::DATE => Value::Date(value.date()).encode(postgres_type, out, top_level),
            _ => Err(self.wrong_type(postgres_type)),
        }
    }

    fn encode_time(
        &self,
        value: NaiveTime,
        postgres_type: &Type,
        out: &mut BytesMut,
        top_level: bool,
    ) -> Result<IsNull, BoxError> {
        match *postgres_type {
            Type::TIME => {
                out.put_i64(time_microseconds(value));
                Ok(IsNull::No)
            }
            // A naive time where TIMETZ is required outright (a TIMETZ[] element, a COPY
            // column) gets UTC - what the server's own time -> timetz cast gives in the UTC
            // session every connection runs in.
            Type::TIMETZ => Value::TimeTz(value, FixedOffset::east_opt(0).expect("zero is a valid offset")).encode(
                postgres_type,
                out,
                top_level,
            ),
            _ => Err(self.wrong_type(postgres_type)),
        }
    }
}

/// True for the catalog types whose binary format is an unsigned 32-bit integer (`oid`, the
/// `reg*` aliases, `xid`, `cid`).
fn is_unsigned_32_bit_type(postgres_type: &Type) -> bool {
    matches!(
        *postgres_type,
        Type::OID
            | Type::REGPROC
            | Type::REGPROCEDURE
            | Type::REGOPER
            | Type::REGOPERATOR
            | Type::REGCLASS
            | Type::REGTYPE
            | Type::REGCONFIG
            | Type::REGDICTIONARY
            | Type::REGNAMESPACE
            | Type::REGROLE
            | Type::REGCOLLATION
            | Type::XID
            | Type::CID
    )
}

/// Encodes an integer as `ty`; `None` when `ty` is not a numeric type an integer can bind to.
#[allow(clippy::cast_precision_loss, reason = "an int bound to a float column is rounded, as asyncpg does")]
fn encode_integer(value: i64, postgres_type: &Type, out: &mut BytesMut) -> Option<Result<IsNull, BoxError>> {
    let out_of_range =
        || -> BoxError { format!("integer {value} is out of range for type {}", postgres_type.name()).into() };
    let result = match *postgres_type {
        Type::INT2 => i16::try_from(value).map(|narrowed| out.put_i16(narrowed)).map_err(|_| out_of_range()),
        Type::INT4 => i32::try_from(value).map(|narrowed| out.put_i32(narrowed)).map_err(|_| out_of_range()),
        Type::INT8 => {
            out.put_i64(value);
            Ok(())
        }
        Type::NUMERIC => {
            return Some(
                DecimalDigits::parse(&value.to_string())
                    .map_err(BoxError::from)
                    .and_then(|digits| digits.write_binary(out)),
            );
        }
        Type::FLOAT4 => {
            out.put_f32(value as f32);
            Ok(())
        }
        Type::FLOAT8 => {
            out.put_f64(value as f64);
            Ok(())
        }
        Type::XID8 => u64::try_from(value).map(|narrowed| out.put_u64(narrowed)).map_err(|_| out_of_range()),
        _ if is_unsigned_32_bit_type(postgres_type) => {
            u32::try_from(value).map(|narrowed| out.put_u32(narrowed)).map_err(|_| out_of_range())
        }
        _ => return None,
    };
    Some(result.map(|()| IsNull::No))
}

/// Python's `datetime.isoformat()` text for a naive datetime.
fn iso_datetime_text(value: &NaiveDateTime) -> String {
    format!("{}T{}", value.format("%Y-%m-%d"), iso_time_text(value.time()))
}

/// Python's `time.isoformat()` text - microseconds only when non-zero.
fn iso_time_text(value: NaiveTime) -> String {
    if value.nanosecond() == 0 {
        value.format("%H:%M:%S").to_string()
    } else {
        value.format("%H:%M:%S%.6f").to_string()
    }
}

impl ToSql for Value {
    fn to_sql(&self, postgres_type: &Type, out: &mut BytesMut) -> Result<IsNull, BoxError> {
        self.encode(postgres_type, out, true)
    }

    fn accepts(_ty: &Type) -> bool {
        // Every value is checked against the resolved type in `encode` itself.
        true
    }

    /// Must mirror `encode`'s own top-level branching - the server is told this format before it
    /// sees the bytes. A string is sent in the text format (parsed by the type's own input
    /// function) except where `encode` writes a binary encoding for it (UUID, json, jsonb).
    fn encode_format(&self, postgres_type: &Type) -> Format {
        let postgres_type = domain_base_type(postgres_type);
        match self {
            Value::Text(_) if !matches!(*postgres_type, Type::UUID | Type::JSON | Type::JSONB) => Format::Text,
            Value::Array(_) if is_text_format_array(self, postgres_type) => Format::Text,
            _ => Format::Binary,
        }
    }

    to_sql_checked!();
}

/// A value written where the binary format is required outright (a COPY BINARY column).
#[derive(Debug)]
pub(crate) struct BinaryParameter<'a>(pub(crate) &'a Value);

impl ToSql for BinaryParameter<'_> {
    fn to_sql(&self, postgres_type: &Type, out: &mut BytesMut) -> Result<IsNull, BoxError> {
        self.0.encode(postgres_type, out, false)
    }

    fn accepts(_ty: &Type) -> bool {
        true
    }

    to_sql_checked!();
}

/// A value's raw binary wire bytes, sent back to the server unchanged (see `ServerTextForms`).
#[derive(Debug)]
pub(crate) struct RawWireValue<'a>(pub(crate) &'a [u8]);

impl ToSql for RawWireValue<'_> {
    fn to_sql(&self, _ty: &Type, out: &mut BytesMut) -> Result<IsNull, BoxError> {
        out.extend_from_slice(self.0);
        Ok(IsNull::No)
    }

    fn accepts(_ty: &Type) -> bool {
        true
    }

    to_sql_checked!();
}
