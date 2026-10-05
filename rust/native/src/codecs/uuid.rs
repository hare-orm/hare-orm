//! A `UUIDField`: read as a plain `uuid.UUID`, written as its canonical text.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::PyString;
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::options::Options;
use crate::python::objects;
use crate::python::references::PythonReferences;

/// The value of canonical UUID text - 32 hex digits, or 36 with hyphens at 8, 13, 18 and 23.
pub fn parse_uuid(text: &str) -> Option<u128> {
    let bytes = text.as_bytes();
    let mut value: u128 = 0;
    let mut digit_count = 0;
    match bytes.len() {
        32 => {}
        36 => {
            if [8, 13, 18, 23].iter().any(|&index| bytes[index] != b'-') {
                return None;
            }
        }
        _ => return None,
    }
    for (index, &byte) in bytes.iter().enumerate() {
        if bytes.len() == 36 && matches!(index, 8 | 13 | 18 | 23) {
            continue;
        }
        let digit = char::from(byte).to_digit(16)?;
        value = (value << 4) | u128::from(digit);
        digit_count += 1;
    }
    (digit_count == 32).then_some(value)
}

/// `str(uuid)` of a 128-bit value - lowercase, hyphenated.
pub fn format_uuid(value: u128) -> String {
    let hex = format!("{value:032x}");
    format!("{}-{}-{}-{}-{}", &hex[0..8], &hex[8..12], &hex[12..16], &hex[16..20], &hex[20..32])
}

pub struct UuidRead {
    fallback: Py<PyAny>,
}

impl UuidRead {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(UuidRead { fallback: options.get_object("fallback")? })
    }

    pub fn read<'py>(&self, raw: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = raw.py();
        let uuid_type = objects::uuid_type(py)?;
        if raw.is_none() || raw.get_type().is(uuid_type) {
            return Ok(raw);
        }
        if raw.is_instance(uuid_type)? {
            // A driver's own subclass - a plain UUID of the same value.
            return objects::new_uuid(py, raw.getattr(intern!(py, "int"))?.extract()?);
        }
        if let Ok(text) = raw.cast::<PyString>() {
            if let Some(value) = text.to_str().ok().and_then(parse_uuid) {
                return objects::new_uuid(py, value);
            }
        }
        self.fallback.bind(py).call1((raw,))
    }
}

pub struct UuidWrite {
    fallback: Py<PyAny>,
    validate: Option<Py<PyAny>>,
    null: bool,
    /// Whether a UUID is written as itself, for a driver binding UUIDs - else as its text.
    as_object: bool,
}

impl UuidWrite {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(UuidWrite {
            fallback: options.get_object("fallback")?,
            validate: options.get_optional_object("validate")?,
            null: options.get_bool("null")?,
            as_object: options.get_bool("as_object")?,
        })
    }

    pub fn write<'py>(&self, value: Bound<'py, PyAny>, instance: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() && self.null {
            return Ok(value);
        }
        if !value.get_type().is(objects::uuid_type(py)?) {
            return self.fallback.bind(py).call1((value, instance));
        }
        if let Some(validate) = &self.validate {
            validate.bind(py).call1((&value,))?;
        }
        if self.as_object {
            return Ok(value);
        }
        let integer: u128 = value.getattr(intern!(py, "int"))?.extract()?;
        Ok(PyString::new(py, &format_uuid(integer)).into_any())
    }
}

impl PythonReferences for UuidRead {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        Ok(())
    }
}

impl PythonReferences for UuidWrite {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        visit.call(&self.validate)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_canonical_text() {
        let value = 0x1234_5678_9abc_def0_1234_5678_9abc_def0_u128;
        assert_eq!(parse_uuid("12345678-9abc-def0-1234-56789abcdef0"), Some(value));
        assert_eq!(parse_uuid("123456789ABCDEF0123456789ABCDEF0"), Some(value));
        assert_eq!(format_uuid(value), "12345678-9abc-def0-1234-56789abcdef0");
    }

    #[test]
    fn rejects_other_text() {
        assert_eq!(parse_uuid("{12345678-9abc-def0-1234-56789abcdef0}"), None);
        assert_eq!(parse_uuid("12345678-9abc-def0-1234-56789abcdefg"), None);
        assert_eq!(parse_uuid("123456789-abc-def0-1234-56789abcdef0"), None);
        assert_eq!(parse_uuid("+2345678-9abc-def0-1234-56789abcdef0"), None);
        assert_eq!(parse_uuid(""), None);
    }
}
