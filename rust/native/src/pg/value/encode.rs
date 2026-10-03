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
use crate::pg::types::range::encode_range;
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
            Value::Bool(v) => Some(if *v { "true" } else { "false" }.to_string()),
            Value::Int(v) => Some(v.to_string()),
            Value::BigInt(v) | Value::Decimal(v) | Value::Json(v) => Some(v.clone()),
            Value::Float(v) => Some(v.to_string()),
            Value::Uuid(v) => Some(v.to_string()),
            Value::NonFiniteDecimal(v) => Some(v.as_text().to_string()),
            Value::Timestamp(v) => Some(v.format("%Y-%m-%d %H:%M:%S%.6f").to_string()),
            Value::TimestampTz(v) => Some(v.to_rfc3339()),
            Value::Date(v) => Some(v.format("%Y-%m-%d").to_string()),
            Value::Time(v) => Some(v.format("%H:%M:%S%.6f").to_string()),
            Value::TimeTz(v, offset) => Some(format!("{}{}", v.format("%H:%M:%S%.6f"), offset)),
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

    pub(crate) fn wrong_type(&self, ty: &Type) -> BoxError {
        format!("cannot bind a {} value to a parameter of type {}", self.variant_type_name(), ty.name()).into()
    }

    /// Appends the JSON form of a value bound as an element of a list sent to a json/jsonb
    /// parameter - mirrors how a Python value nested in a dict is serialized.
    pub(crate) fn write_json(&self, out: &mut String) -> Result<(), String> {
        match self {
            Value::Null => out.push_str("null"),
            Value::Bool(v) => out.push_str(if *v { "true" } else { "false" }),
            Value::Int(v) => write!(out, "{v}").expect("writing into a String never fails"),
            Value::BigInt(v) | Value::Json(v) => out.push_str(v),
            Value::Float(v) => write_json_float(*v, out)?,
            Value::Text(v) | Value::Decimal(v) => write_json_string(v, out),
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
            Value::Uuid(v) => write_json_string(&v.to_string(), out),
            Value::NonFiniteDecimal(v) => write_json_string(v.as_text(), out),
            Value::Timestamp(v) => write_json_string(&iso_datetime_text(v), out),
            Value::TimestampTz(v) => write_json_string(&format!("{}+00:00", iso_datetime_text(&v.naive_utc())), out),
            Value::Date(v) => write_json_string(&v.format("%Y-%m-%d").to_string(), out),
            Value::Time(v) => write_json_string(&iso_time_text(*v), out),
            Value::TimeTz(v, offset) => write_json_string(&format!("{}{}", iso_time_text(*v), offset), out),
            Value::Bytes(_) | Value::Range(_) | Value::Interval(_) | Value::Network(_) | Value::Composite(_) => {
                return Err(format!("a {} value is not JSON serialisable", self.variant_type_name()));
            }
        }
        Ok(())
    }

    /// Encodes the value as a parameter of type `ty`, checking that the two match.
    /// `top_level` is true for a bind parameter whose wire format `encode_format` negotiated, and
    /// false wherever the binary format is required (array elements, range bounds, COPY).
    pub(crate) fn encode(&self, ty: &Type, out: &mut BytesMut, top_level: bool) -> Result<IsNull, BoxError> {
        let ty = domain_base_type(ty);
        if matches!(*ty, Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::NAME | Type::UNKNOWN) {
            if let Some(text) = self.as_pg_text() {
                out.extend_from_slice(text.as_bytes());
                return Ok(IsNull::No);
            }
        }
        match self {
            Value::Null => Ok(IsNull::Yes),
            Value::Bool(v) => match *ty {
                Type::BOOL => {
                    out.put_u8(u8::from(*v));
                    Ok(IsNull::No)
                }
                Type::INT2 | Type::INT4 | Type::INT8 | Type::NUMERIC | Type::FLOAT4 | Type::FLOAT8 => {
                    Value::Int(i64::from(*v)).encode(ty, out, top_level)
                }
                _ => Err(self.wrong_type(ty)),
            },
            Value::Int(v) => encode_integer(*v, ty, out).ok_or_else(|| self.wrong_type(ty))?,
            Value::BigInt(text) => match *ty {
                Type::NUMERIC => DecimalDigits::parse(text)?.write_binary(out),
                Type::FLOAT4 => {
                    out.put_f32(text.parse::<f32>()?);
                    Ok(IsNull::No)
                }
                Type::FLOAT8 => {
                    out.put_f64(text.parse::<f64>()?);
                    Ok(IsNull::No)
                }
                _ if encode_integer(0, ty, &mut BytesMut::new()).is_some() => {
                    Err(format!("integer {text} is out of range for type {}", ty.name()).into())
                }
                _ => Err(self.wrong_type(ty)),
            },
            Value::Float(v) => match *ty {
                Type::FLOAT4 => {
                    out.put_f32(*v as f32);
                    Ok(IsNull::No)
                }
                Type::FLOAT8 => {
                    out.put_f64(*v);
                    Ok(IsNull::No)
                }
                Type::NUMERIC if v.is_finite() => DecimalDigits::from_f64(*v).write_binary(out),
                Type::NUMERIC => Ok(NonFiniteNumeric::from_f64(*v).write_binary(out)),
                _ => Err(self.wrong_type(ty)),
            },
            Value::Text(v) => match *ty {
                Type::UUID => {
                    out.extend_from_slice(uuid::Uuid::parse_str(v)?.as_bytes());
                    Ok(IsNull::No)
                }
                // A pre-encoded JSON string is sent as-is - json's binary format is its text,
                // jsonb's is a version byte followed by the text. The server validates it.
                Type::JSON => {
                    out.extend_from_slice(v.as_bytes());
                    Ok(IsNull::No)
                }
                Type::JSONB => {
                    out.put_u8(1);
                    out.extend_from_slice(v.as_bytes());
                    Ok(IsNull::No)
                }
                // At the top level `encode_format` declared the text format, so the server parses
                // the string with the type's own input function.
                _ if top_level || member_type_is_text_compatible(ty) => {
                    out.extend_from_slice(v.as_bytes());
                    Ok(IsNull::No)
                }
                _ => Err(format!(
                    "cannot bind a string where a binary {} value is required (an array element, a range bound \
                     or a COPY column); pass a value of the column's native type instead",
                    ty.name()
                )
                .into()),
            },
            Value::Bytes(v) => match *ty {
                Type::BYTEA => {
                    out.extend_from_slice(v);
                    Ok(IsNull::No)
                }
                _ => Err(self.wrong_type(ty)),
            },
            Value::Json(text) => match *ty {
                Type::JSON => {
                    out.extend_from_slice(text.as_bytes());
                    Ok(IsNull::No)
                }
                Type::JSONB => {
                    out.put_u8(1);
                    out.extend_from_slice(text.as_bytes());
                    Ok(IsNull::No)
                }
                _ => Err(self.wrong_type(ty)),
            },
            Value::Array(items) => {
                // A list bound to a json/jsonb parameter is a JSON array, not a Postgres array.
                if matches!(*ty, Type::JSON | Type::JSONB) {
                    let mut text = String::new();
                    self.write_json(&mut text)?;
                    return Value::Json(text).encode(ty, out, top_level);
                }
                if !matches!(ty.kind(), TypeShape::Array(_)) {
                    return Err(format!("cannot bind a list to a parameter of non-array type {}", ty.name()).into());
                }
                if top_level && is_text_format_array(self, ty) {
                    let mut literal = String::new();
                    write_text_array_literal(items, &mut literal);
                    out.extend_from_slice(literal.as_bytes());
                    return Ok(IsNull::No);
                }
                encode_array(items, ty, out)
            }
            Value::Range(r) => encode_range(r, ty, out),
            Value::Uuid(v) => match *ty {
                Type::UUID => {
                    out.extend_from_slice(v.as_bytes());
                    Ok(IsNull::No)
                }
                _ => Err(self.wrong_type(ty)),
            },
            Value::Decimal(text) => match *ty {
                Type::NUMERIC => DecimalDigits::parse(text)?.write_binary(out),
                Type::FLOAT4 => {
                    out.put_f32(text.parse::<f32>()?);
                    Ok(IsNull::No)
                }
                Type::FLOAT8 => {
                    out.put_f64(text.parse::<f64>()?);
                    Ok(IsNull::No)
                }
                _ => Err(self.wrong_type(ty)),
            },
            Value::NonFiniteDecimal(v) => match *ty {
                Type::NUMERIC => Ok(v.write_binary(out)),
                Type::FLOAT4 => {
                    out.put_f32(v.to_f64() as f32);
                    Ok(IsNull::No)
                }
                Type::FLOAT8 => {
                    out.put_f64(v.to_f64());
                    Ok(IsNull::No)
                }
                _ => Err(format!("cannot bind decimal {} to a parameter of type {}", v.as_text(), ty.name()).into()),
            },
            Value::Timestamp(v) => match *ty {
                Type::TIMESTAMP => match infinite_timestamp_wire_value(v) {
                    Some(infinity) => {
                        out.put_i64(infinity);
                        Ok(IsNull::No)
                    }
                    None => v.to_sql(ty, out),
                },
                Type::TIMESTAMPTZ => {
                    if let Some(infinity) = infinite_timestamp_wire_value(v) {
                        out.put_i64(infinity);
                        return Ok(IsNull::No);
                    }
                    // Interpreted as the client machine's local wall-clock time, like asyncpg.
                    let aware = Local
                        .from_local_datetime(v)
                        .single()
                        .ok_or("ambiguous or nonexistent local datetime (DST transition)")?;
                    aware.with_timezone(&Utc).to_sql(ty, out)
                }
                // A naive datetime bound to a date keeps its date, as asyncpg does.
                Type::DATE => Value::Date(v.date()).encode(ty, out, top_level),
                _ => Err(self.wrong_type(ty)),
            },
            Value::TimestampTz(v) => match *ty {
                Type::TIMESTAMPTZ => match infinite_timestamp_wire_value(&v.naive_utc()) {
                    Some(infinity) => {
                        out.put_i64(infinity);
                        Ok(IsNull::No)
                    }
                    None => v.to_sql(ty, out),
                },
                _ => Err(self.wrong_type(ty)),
            },
            Value::Date(v) => match *ty {
                Type::DATE => match infinite_date_wire_value(*v) {
                    Some(infinity) => {
                        out.put_i32(infinity);
                        Ok(IsNull::No)
                    }
                    None => v.to_sql(ty, out),
                },
                _ => Err(self.wrong_type(ty)),
            },
            Value::Time(v) => match *ty {
                Type::TIME => {
                    out.put_i64(time_microseconds(*v));
                    Ok(IsNull::No)
                }
                // A naive time where TIMETZ is required outright (a TIMETZ[] element, a COPY
                // column) gets UTC - what the server's own time -> timetz cast gives in the UTC
                // session every connection runs in.
                Type::TIMETZ => Value::TimeTz(*v, FixedOffset::east_opt(0).expect("zero is a valid offset"))
                    .encode(ty, out, top_level),
                _ => Err(self.wrong_type(ty)),
            },
            Value::TimeTz(v, offset) => match *ty {
                Type::TIMETZ => {
                    out.put_i64(time_microseconds(*v));
                    // The wire's zone field is seconds WEST of UTC.
                    out.put_i32(-offset.local_minus_utc());
                    Ok(IsNull::No)
                }
                _ => Err(self.wrong_type(ty)),
            },
            Value::Interval(interval) => match *ty {
                Type::INTERVAL => {
                    out.put_i64(interval.microseconds);
                    out.put_i32(interval.days);
                    out.put_i32(interval.months);
                    Ok(IsNull::No)
                }
                _ => Err(self.wrong_type(ty)),
            },
            Value::Network(_) | Value::Composite(_) => Err(self.wrong_type(ty)),
        }
    }
}

/// True for the catalog types whose binary format is an unsigned 32-bit integer (`oid`, the
/// `reg*` aliases, `xid`, `cid`).
fn is_unsigned_32_bit_type(ty: &Type) -> bool {
    matches!(
        *ty,
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
fn encode_integer(value: i64, ty: &Type, out: &mut BytesMut) -> Option<Result<IsNull, BoxError>> {
    let out_of_range = || -> BoxError { format!("integer {value} is out of range for type {}", ty.name()).into() };
    let result = match *ty {
        Type::INT2 => i16::try_from(value).map(|v| out.put_i16(v)).map_err(|_| out_of_range()),
        Type::INT4 => i32::try_from(value).map(|v| out.put_i32(v)).map_err(|_| out_of_range()),
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
        Type::XID8 => u64::try_from(value).map(|v| out.put_u64(v)).map_err(|_| out_of_range()),
        _ if is_unsigned_32_bit_type(ty) => u32::try_from(value).map(|v| out.put_u32(v)).map_err(|_| out_of_range()),
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
    fn to_sql(&self, ty: &Type, out: &mut BytesMut) -> Result<IsNull, BoxError> {
        self.encode(ty, out, true)
    }

    fn accepts(_ty: &Type) -> bool {
        // Every value is checked against the resolved type in `encode` itself.
        true
    }

    /// Must mirror `encode`'s own top-level branching - the server is told this format before it
    /// sees the bytes. A string is sent in the text format (parsed by the type's own input
    /// function) except where `encode` writes a binary encoding for it (UUID, json, jsonb).
    fn encode_format(&self, ty: &Type) -> Format {
        let ty = domain_base_type(ty);
        match self {
            Value::Text(_) if !matches!(*ty, Type::UUID | Type::JSON | Type::JSONB) => Format::Text,
            Value::Array(_) if is_text_format_array(self, ty) => Format::Text,
            _ => Format::Binary,
        }
    }

    to_sql_checked!();
}

/// A value written where the binary format is required outright (a COPY BINARY column).
#[derive(Debug)]
pub(crate) struct BinaryParameter<'a>(pub(crate) &'a Value);

impl ToSql for BinaryParameter<'_> {
    fn to_sql(&self, ty: &Type, out: &mut BytesMut) -> Result<IsNull, BoxError> {
        self.0.encode(ty, out, false)
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
