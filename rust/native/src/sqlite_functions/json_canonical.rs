//! `JSONField` equality on SQLite: the canonical text of a JSON value - `json.dumps(value,
//! sort_keys=True, separators=(",", ":"), ensure_ascii=False)` of what `json.loads()` reads, an
//! integral float written as an int - so key order, whitespace and `1` against `1.0` don't matter.

use std::fmt::Write;

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyFloat, PyInt, PyString};
use pyo3::{PyTraverseError, PyVisit};
use serde_json::{Number, Value};

/// Writes `text` as `json.dumps(ensure_ascii=False)` writes a str.
pub fn write_json_string(text: &str, out: &mut String) {
    out.push('"');
    let mut copied_up_to = 0;
    for (index, &byte) in text.as_bytes().iter().enumerate() {
        let escape = match byte {
            b'"' => "\\\"",
            b'\\' => "\\\\",
            b'\n' => "\\n",
            b'\r' => "\\r",
            b'\t' => "\\t",
            0x08 => "\\b",
            0x0c => "\\f",
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
}

/// Writes a finite float as `repr()` writes it: its shortest round-tripping digits, in scientific
/// notation (`1e-05`, `1.5e+16`) when the decimal point falls 4 or more places before the first
/// digit or more than 16 after it.
fn write_float_repr(number: f64, out: &mut String) {
    let mut buffer = ryu::Buffer::new();
    let text = buffer.format_finite(number.abs());
    let (mantissa, exponent) = match text.split_once('e') {
        Some((mantissa, exponent)) => (mantissa, exponent.parse::<i32>().expect("ryu writes an integer exponent")),
        None => (text, 0),
    };
    let (whole, fraction) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    let mut digits = String::with_capacity(whole.len() + fraction.len());
    let mut leading_zeros = 0;
    for character in whole.chars().chain(fraction.chars()) {
        if digits.is_empty() && character == '0' {
            leading_zeros += 1;
        } else {
            digits.push(character);
        }
    }
    let digits = digits.trim_end_matches('0');
    // The decimal point's position after the first digit, as CPython's `decpt` counts it.
    let point = whole.len() as i32 - leading_zeros + exponent;
    if number.is_sign_negative() {
        out.push('-');
    }
    if point <= -4 || point > 16 {
        out.push_str(&digits[..1]);
        if digits.len() > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        let shown_exponent = point - 1;
        let _ = write!(out, "e{}{:02}", if shown_exponent < 0 { '-' } else { '+' }, shown_exponent.unsigned_abs());
    } else if point <= 0 {
        out.push_str("0.");
        for _ in 0..-point {
            out.push('0');
        }
        out.push_str(digits);
    } else {
        let point = point as usize;
        if digits.len() <= point {
            out.push_str(digits);
            for _ in digits.len()..point {
                out.push('0');
            }
            out.push_str(".0");
        } else {
            out.push_str(&digits[..point]);
            out.push('.');
            out.push_str(&digits[point..]);
        }
    }
}

/// Writes a float as the canonical text writes it - an integral one as its exact int; false for
/// one this doesn't write (not finite, an integer past 128 bits).
fn write_float(number: f64, out: &mut String) -> bool {
    if !number.is_finite() {
        return false;
    }
    if number.fract() != 0.0 {
        write_float_repr(number, out);
        return true;
    }
    if number.abs() >= 1.7e38 {
        return false;
    }
    #[expect(clippy::cast_possible_truncation, reason = "an integral float below 2**127 is an i128 exactly")]
    let integer = number as i128;
    let _ = write!(out, "{integer}");
    true
}

/// Writes a JSON number as `json.loads()` reads it, then canonical; false for one this doesn't write.
fn write_number(number: &Number, out: &mut String) -> bool {
    let text = number.as_str();
    if text.bytes().any(|byte| matches!(byte, b'.' | b'e' | b'E')) {
        return text.parse::<f64>().is_ok_and(|float| write_float(float, out));
    }
    match text.parse::<i128>() {
        Ok(integer) => {
            let _ = write!(out, "{integer}");
            true
        }
        Err(_) => false,
    }
}

/// Writes the canonical text of a JSON value; false when it holds a number this doesn't write.
fn write_value(value: &Value, out: &mut String) -> bool {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(flag) => out.push_str(if *flag { "true" } else { "false" }),
        Value::Number(number) => return write_number(number, out),
        Value::String(text) => write_json_string(text, out),
        Value::Array(elements) => {
            out.push('[');
            for (index, element) in elements.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                if !write_value(element, out) {
                    return false;
                }
            }
            out.push(']');
        }
        Value::Object(members) => {
            // serde_json keeps an object's keys sorted - by code point, as Python sorts str keys.
            out.push('{');
            for (index, (key, member)) in members.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                write_json_string(key, out);
                out.push(':');
                if !write_value(member, out) {
                    return false;
                }
            }
            out.push('}');
        }
    }
    true
}

/// The canonical JSON text function; a value it doesn't write itself goes to the Python one given.
#[pyclass(frozen, module = "rust.native.sqlite_functions")]
pub struct JsonCanonical {
    canonicalize_in_python: Py<PyAny>,
}

#[pymethods]
impl JsonCanonical {
    #[new]
    fn new(canonicalize_in_python: Py<PyAny>) -> Self {
        JsonCanonical { canonicalize_in_python }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.canonicalize_in_python)
    }

    /// The canonical text of JSON text or of a number; None stays None.
    fn canonicalize<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() {
            return Ok(py.None().into_bound(py));
        }
        let mut out = String::new();
        let written = if let Ok(text) = value.cast::<PyString>() {
            serde_json::from_str::<Value>(text.to_str()?).is_ok_and(|parsed| write_value(&parsed, &mut out))
        } else if let Ok(bytes) = value.cast::<PyBytes>() {
            serde_json::from_slice::<Value>(bytes.as_bytes()).is_ok_and(|parsed| write_value(&parsed, &mut out))
        } else if value.is_instance_of::<PyInt>() {
            value.extract::<i128>().is_ok_and(|integer| write!(out, "{integer}").is_ok())
        } else if let Ok(number) = value.cast::<PyFloat>() {
            write_float(number.value(), &mut out)
        } else {
            false
        };
        if written {
            return Ok(PyString::new(py, &out).into_any());
        }
        self.canonicalize_in_python.bind(py).call1((value,))
    }
}
