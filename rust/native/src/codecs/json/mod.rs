//! A `JSONField`. Reading decodes the column's text with the field's decoder (orjson directly when the
//! decoder is hare's own and the text holds no long integer); writing encodes a value made only of
//! dicts, lists, tuples, str, int, float, bool and None into the text orjson writes, checking what a
//! JSON column can't store on the same pass. Anything else goes through the field.

use crate::python::ffi;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};
use std::fmt::Write;

mod json_read;
mod json_write;

pub use json_read::JsonRead;
pub use json_write::JsonWrite;

/// `JSON_LONG_INTEGER_MIN_DIGITS` - from this many digits in a row `JsonCodec.loads` reads the text
/// with the standard library, which reads an integer of any size exactly.
const LONG_INTEGER_MIN_DIGITS: usize = 19;

/// The nesting orjson encodes - deeper lists and dicts fail there, a deeper tuple overflows its stack.
pub const MAXIMUM_DEPTH: usize = 254;

/// Whether `text` holds a run of `LONG_INTEGER_MIN_DIGITS` ASCII digits - an integer orjson might not
/// read exactly (`JsonCodec.has_long_integer`).
pub fn has_long_digit_run(text: &[u8]) -> bool {
    if text.len() < LONG_INTEGER_MIN_DIGITS {
        return false;
    }
    let mut run = 0;
    for byte in text {
        if byte.is_ascii_digit() {
            run += 1;
            if run >= LONG_INTEGER_MIN_DIGITS {
                return true;
            }
        } else {
            run = 0;
        }
    }
    false
}

/// A finite float as orjson writes it: the shortest digits that read back as it, the closest to it
/// among them - fixed-point for a decimal exponent from -5 to 15 (`0.00001`, `123.0`), scientific
/// otherwise (`1e-6`, `1.5e+16`). ryu chooses between the two the same way; only a positive exponent
/// gets its `+` here.
pub fn write_float(number: f64, out: &mut String) {
    let mut buffer = ryu::Buffer::new();
    let text = buffer.format_finite(number);
    match text.split_once('e') {
        Some((mantissa, exponent)) if !exponent.starts_with('-') => {
            out.push_str(mantissa);
            out.push_str("e+");
            out.push_str(exponent);
        }
        _ => out.push_str(text),
    }
}

/// `text` as a JSON string, escaped as orjson escapes it; false for a null byte, which a JSON column
/// can't store.
pub fn write_string(text: &str, out: &mut String) -> bool {
    out.push('"');
    if !text.bytes().any(|byte| byte < 0x20 || byte == b'"' || byte == b'\\') {
        out.push_str(text);
        out.push('"');
        return true;
    }
    // Escapes are ASCII, never a byte of a multi-byte character: the text between them is copied
    // as it is.
    let mut copied_up_to = 0;
    for (index, &byte) in text.as_bytes().iter().enumerate() {
        let escape = match byte {
            0 => return false,
            b'"' => "\\\"",
            b'\\' => "\\\\",
            0x08 => "\\b",
            b'\t' => "\\t",
            b'\n' => "\\n",
            0x0c => "\\f",
            b'\r' => "\\r",
            control if control < 0x20 => {
                out.push_str(&text[copied_up_to..index]);
                let _ = write!(out, "\\u{control:04x}");
                copied_up_to = index + 1;
                continue;
            }
            _ => continue,
        };
        out.push_str(&text[copied_up_to..index]);
        out.push_str(escape);
        copied_up_to = index + 1;
    }
    out.push_str(&text[copied_up_to..]);
    out.push('"');
    true
}

/// Writes `value` as JSON text; false when the value holds anything this encoder leaves to the
/// field - another type, a null byte, NaN or infinity, an int past 64 bits, deep nesting.
fn write_value(value: &Bound<'_, PyAny>, out: &mut String, depth: usize) -> bool {
    match ffi::get_json_type(value) {
        ffi::JsonType::None => out.push_str("null"),
        ffi::JsonType::Bool => out.push_str(if value.is_truthy().unwrap_or(false) { "true" } else { "false" }),
        ffi::JsonType::Int => return write_integer(value, out),
        ffi::JsonType::Float => {
            let Ok(number) = value.extract::<f64>() else {
                return false;
            };
            if !number.is_finite() {
                return false;
            }
            write_float(number, out);
        }
        ffi::JsonType::Str => {
            let Ok(text) = value.cast::<PyString>().map_err(PyErr::from).and_then(|text| text.to_str()) else {
                return false;
            };
            return write_string(text, out);
        }
        ffi::JsonType::Dict => return depth < MAXIMUM_DEPTH && write_dict(value, out, depth),
        ffi::JsonType::List => return depth < MAXIMUM_DEPTH && write_list(value, out, depth),
        ffi::JsonType::Tuple => return depth < MAXIMUM_DEPTH && write_tuple(value, out, depth),
        ffi::JsonType::Other => return false,
    }
    true
}

/// Writes an int of at most 64 bits; false for a larger one.
fn write_integer(value: &Bound<'_, PyAny>, out: &mut String) -> bool {
    let mut buffer = itoa::Buffer::new();
    if let Some(number) = ffi::get_i64(value) {
        out.push_str(buffer.format(number));
    } else if let Ok(number) = value.extract::<u64>() {
        out.push_str(buffer.format(number));
    } else {
        return false;
    }
    true
}

/// Writes a dict with string keys as a JSON object.
fn write_dict(value: &Bound<'_, PyAny>, out: &mut String, depth: usize) -> bool {
    let Ok(dict) = value.cast::<PyDict>() else {
        return false;
    };
    out.push('{');
    let mut is_first = true;
    let written = ffi::all_dict_items(dict, |key, item| {
        if !std::mem::take(&mut is_first) {
            out.push(',');
        }
        if !matches!(ffi::get_json_type(key), ffi::JsonType::Str) {
            return false;
        }
        let Ok(key) = key.cast::<PyString>().map_err(PyErr::from).and_then(|key| key.to_str()) else {
            return false;
        };
        if !write_string(key, out) {
            return false;
        }
        out.push(':');
        write_value(item, out, depth + 1)
    });
    if !written {
        return false;
    }
    out.push('}');
    true
}

fn write_list(value: &Bound<'_, PyAny>, out: &mut String, depth: usize) -> bool {
    let Ok(list) = value.cast::<PyList>() else {
        return false;
    };
    out.push('[');
    let mut is_first = true;
    let written = ffi::all_list_items(list, |item| {
        if !std::mem::take(&mut is_first) {
            out.push(',');
        }
        write_value(item, out, depth + 1)
    });
    if !written {
        return false;
    }
    out.push(']');
    true
}

fn write_tuple(value: &Bound<'_, PyAny>, out: &mut String, depth: usize) -> bool {
    let Ok(tuple) = value.cast::<PyTuple>() else {
        return false;
    };
    out.push('[');
    let mut is_first = true;
    let written = ffi::all_tuple_items(tuple, |item| {
        if !std::mem::take(&mut is_first) {
            out.push(',');
        }
        write_value(item, out, depth + 1)
    });
    if !written {
        return false;
    }
    out.push(']');
    true
}

/// The JSON text of `value`, None when the field has to encode it.
pub fn encode(value: &Bound<'_, PyAny>) -> Option<String> {
    let mut out = String::new();
    write_value(value, &mut out, 0).then_some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn float_text(repr: &str) -> String {
        let mut out = String::new();
        write_float(repr.parse().expect("a float literal"), &mut out);
        out
    }

    #[test]
    fn writes_floats_as_orjson() {
        let cases = [
            ("1000000000000000.0", "1000000000000000.0"),
            ("1e+16", "1e+16"),
            ("1.2345678901234568e+16", "1.2345678901234568e+16"),
            ("0.0001", "0.0001"),
            ("1e-05", "0.00001"),
            ("1e-06", "1e-6"),
            ("1.5e-05", "0.000015"),
            ("0.00012345", "0.00012345"),
            ("123.0", "123.0"),
            ("123456789.123", "123456789.123"),
            ("153838026194641.12", "153838026194641.12"),
            ("-1e+16", "-1e+16"),
            ("-1e-07", "-1e-7"),
            ("2.5e-308", "2.5e-308"),
            ("1.7976931348623157e+308", "1.7976931348623157e+308"),
            ("0.5", "0.5"),
            ("-0.0", "-0.0"),
            ("0.0", "0.0"),
            ("100.0", "100.0"),
        ];
        for (repr, expected) in cases {
            assert_eq!(float_text(repr), expected, "{repr}");
        }
    }

    #[test]
    fn escapes_strings_as_orjson() {
        let mut out = String::new();
        assert!(write_string("\u{8}\t\n\u{c}\r\"\\/\u{1b}\u{7f}é", &mut out));
        assert_eq!(out, "\"\\b\\t\\n\\f\\r\\\"\\\\/\\u001b\u{7f}é\"");
        assert!(!write_string("a\0b", &mut String::new()));
    }

    #[test]
    fn finds_long_digit_runs() {
        assert!(has_long_digit_run(b"[1234567890123456789]"));
        assert!(!has_long_digit_run(b"[123456789012345678, 1]"));
    }
}
