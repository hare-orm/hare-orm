//! `json`/`jsonb`: the text the Python side decodes, and JSON text for a Python value bound as JSON.

use pyo3::prelude::*;
use std::fmt::Write as _;

use pyo3::types::{PyBool, PyDate, PyDateTime, PyDict, PyFloat, PyInt, PyList, PyString, PyTime, PyTuple};
use tokio_postgres::types::Type;

use crate::pg::error::{to_pyerr, DriverError};
use crate::pg::value::BoxError;
use crate::python::objects::{decimal_type, uuid_type};

/// Appends `text` as a JSON string literal.
pub(crate) fn write_json_string(text: &str, out: &mut String) {
    out.push('"');
    for character in text.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0C}' => out.push_str("\\f"),
            control if (control as u32) < 0x20 => {
                write!(out, "\\u{:04x}", control as u32).expect("writing into a String never fails");
            }
            other => out.push(other),
        }
    }
    out.push('"');
}

/// Appends a finite float as a JSON number - the shortest text that reads back as the same float.
pub(crate) fn write_json_float(value: f64, out: &mut String) -> Result<(), String> {
    if !value.is_finite() {
        return Err(format!("non-finite float {value} has no JSON representation"));
    }
    write!(out, "{value:?}").expect("writing into a String never fails");
    Ok(())
}

/// Serializes a Python value bound as a JSON parameter to JSON text: dict key order kept, ints of
/// any size and floats exact, datetime/date/time as `isoformat()`, UUID/Decimal as strings.
pub(crate) fn write_python_json(object: &Bound<'_, PyAny>, out: &mut String) -> PyResult<()> {
    let conversion_error = |message: String| to_pyerr(DriverError::Conversion(message));
    if object.is_none() {
        out.push_str("null");
    } else if object.is_instance_of::<PyBool>() {
        out.push_str(if object.extract::<bool>()? { "true" } else { "false" });
    } else if object.is_instance_of::<PyInt>() {
        match object.extract::<i64>() {
            Ok(value) => write!(out, "{value}").expect("writing into a String never fails"),
            Err(_) => out.push_str(&object.call_method0("__index__")?.str()?.to_string()),
        }
    } else if object.is_instance_of::<PyFloat>() {
        write_json_float(object.extract::<f64>()?, out).map_err(conversion_error)?;
    } else if object.is_instance_of::<PyString>() {
        write_json_string(&object.extract::<String>()?, out);
    } else if object.is_instance_of::<PyDateTime>()
        || object.is_instance_of::<PyDate>()
        || object.is_instance_of::<PyTime>()
    {
        write_json_string(&object.call_method0("isoformat")?.extract::<String>()?, out);
    } else if object.is_instance_of::<PyList>() || object.is_instance_of::<PyTuple>() {
        out.push('[');
        for (index, item) in object.try_iter()?.enumerate() {
            if index > 0 {
                out.push(',');
            }
            write_python_json(&item?, out)?;
        }
        out.push(']');
    } else if object.is_instance_of::<PyDict>() {
        out.push('{');
        for (index, (key, item)) in object.cast::<PyDict>()?.iter().enumerate() {
            if index > 0 {
                out.push(',');
            }
            let key_text = if key.is_instance_of::<PyString>() {
                key.extract::<String>()?
            } else if key.is_none() {
                "null".to_string()
            } else if key.is_instance_of::<PyBool>() {
                if key.extract::<bool>()? { "true" } else { "false" }.to_string()
            } else if key.is_instance_of::<PyInt>() {
                key.call_method0("__index__")?.str()?.to_string()
            } else if key.is_instance_of::<PyFloat>() {
                let mut text = String::new();
                write_json_float(key.extract::<f64>()?, &mut text).map_err(conversion_error)?;
                text
            } else {
                return Err(conversion_error(format!(
                    "JSON object keys must be str, int, float, bool or None, not {}",
                    key.get_type().name()?
                )));
            };
            write_json_string(&key_text, out);
            out.push(':');
            write_python_json(&item, out)?;
        }
        out.push('}');
    } else {
        let py = object.py();
        if object.is_instance(uuid_type(py)?.as_any())? || object.is_instance(decimal_type(py)?.as_any())? {
            write_json_string(&object.str()?.to_string(), out);
        } else {
            return Err(conversion_error(format!(
                "value of type {} is not JSON serialisable",
                object.get_type().name()?
            )));
        }
    }
    Ok(())
}

/// A `json`/`jsonb` value as its raw JSON text - the Python side decodes it (`JSONField`, or
/// `orjson` when installed), as it does for asyncpg. jsonb's wire format prefixes the text with a
/// version byte (always 1), plain json's is the text itself.
pub(crate) fn decode_json_text<'a>(postgres_type: &Type, raw: &'a [u8]) -> Result<&'a str, BoxError> {
    let text_bytes = if *postgres_type == Type::JSONB {
        match raw.split_first() {
            Some((1, rest)) => rest,
            Some((other, _)) => return Err(format!("unsupported jsonb wire format version {other}").into()),
            None => return Err("empty jsonb payload".into()),
        }
    } else {
        raw
    };
    Ok(std::str::from_utf8(text_bytes)?)
}

#[cfg(test)]
mod tests {

    use tokio_postgres::types::Type;

    use crate::pg::value::test_support::*;
    use crate::pg::value::Value;

    #[test]
    fn json_text_is_sent_unchanged() {
        let text = "{\"b\": 1, \"a\": 2, \"b\": 3, \"f\": 1.000, \"n\": 123456789012345678901234567890}";
        assert_eq!(encode(&Value::Text(text.to_string()), &Type::JSON).unwrap(), text.as_bytes().to_vec());
        let mut expected = vec![1u8];
        expected.extend_from_slice(text.as_bytes());
        assert_eq!(encode(&Value::Text(text.to_string()), &Type::JSONB).unwrap(), expected);
    }

    #[test]
    fn list_bound_to_json_keeps_float_precision() {
        let value = Value::Array(vec![
            Value::Float(-938_371.956_546_780_1),
            Value::Float(1e300),
            Value::BigInt("12345678901234567890".to_string()),
        ]);
        let raw = encode(&value, &Type::JSON).unwrap();
        assert_eq!(String::from_utf8(raw).unwrap(), "[-938371.9565467801,1e300,12345678901234567890]");
        assert!(encode(&Value::Array(vec![Value::Float(f64::NAN)]), &Type::JSON).is_err());
    }
}
