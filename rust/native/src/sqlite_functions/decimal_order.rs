//! The order of `DecimalField` text on SQLite: by exact value, text that isn't a number after every
//! number by its own text - the decimal collation, its sort key and the decimal text function.

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyFloat, PyString};
use pyo3::{PyTraverseError, PyVisit};

use crate::sqlite_functions::decimal_number::DecimalNumber;
use crate::sqlite_functions::sqlite_value_key::{SqliteValue, BLOB_CLASS, NUMBER_CLASS, TEXT_CLASS};

/// The decimal order's functions; a value they don't read themselves (`Infinity`, text with spaces
/// around it) goes to the Python ones given.
#[pyclass(frozen, module = "rust.native.sqlite_functions")]
pub struct DecimalOrder {
    /// `(value) -> bytes | None`, the key of any value.
    fallback_sort_key: Py<PyAny>,
    /// `(left, right) -> int`, the collation's comparison.
    fallback_compare: Py<PyAny>,
}

/// The key of a TEXT value: a number's after the number marker, any other text's after the text one.
fn get_text_key(text: &str) -> Option<Vec<u8>> {
    let number = DecimalNumber::parse(text)?;
    let mut key = vec![TEXT_CLASS, 0x01];
    number.write_key(&mut key);
    Some(key)
}

/// Whether `Decimal()` can't read a text as a number for sure - a text with anything it might
/// read (a digit of any script, a point, an exponent, an infinity or NaN, spaces, underscores) goes
/// to the Python key.
fn is_plainly_not_a_number(text: &str) -> bool {
    !text.chars().any(|character| {
        !character.is_ascii()
            || character.is_ascii_digit()
            || character.is_ascii_whitespace()
            || matches!(character, '.' | '_' | 'e' | 'E' | 'i' | 'I' | 'n' | 'N' | 's' | 'S')
    })
}

#[pymethods]
impl DecimalOrder {
    #[new]
    fn new(fallback_sort_key: Py<PyAny>, fallback_compare: Py<PyAny>) -> Self {
        DecimalOrder { fallback_sort_key, fallback_compare }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback_sort_key)?;
        visit.call(&self.fallback_compare)
    }

    /// The byte key `ORDER BY` sorts a decimal column's value by, as the collation orders it.
    fn sort_key<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        let key = match SqliteValue::read(value)? {
            Some(SqliteValue::Null) => return Ok(py.None().into_bound(py)),
            Some(SqliteValue::Number(number)) => {
                let mut key = vec![NUMBER_CLASS];
                number.write_key(&mut key);
                Some(key)
            }
            Some(SqliteValue::Text(text)) => get_text_key(text).or_else(|| {
                is_plainly_not_a_number(text).then(|| {
                    let mut key = vec![TEXT_CLASS, 0x02];
                    key.extend_from_slice(text.as_bytes());
                    key
                })
            }),
            Some(SqliteValue::Blob(blob)) => Some([&[BLOB_CLASS], blob].concat()),
            None => None,
        };
        match key {
            Some(key) => Ok(PyBytes::new(py, &key).into_any()),
            None => self.fallback_sort_key.bind(py).call1((value,)),
        }
    }

    /// The collation's comparison of two texts.
    fn compare(&self, left: &Bound<'_, PyString>, right: &Bound<'_, PyString>) -> PyResult<i32> {
        if let (Some(left_key), Some(right_key)) = (get_text_key(left.to_str()?), get_text_key(right.to_str()?)) {
            return Ok(left_key.cmp(&right_key) as i32);
        }
        self.fallback_compare.bind(left.py()).call1((left, right))?.extract()
    }

    /// The exact decimal text of a value - a double at its shortest round-tripping digits, text
    /// unchanged, None for NULL or a BLOB.
    #[staticmethod]
    fn get_decimal_text<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() || value.is_instance_of::<PyString>() {
            return Ok(value.clone());
        }
        if value.is_instance_of::<PyBytes>() {
            return Ok(py.None().into_bound(py));
        }
        if value.is_instance_of::<PyFloat>() {
            return Ok(value.repr()?.into_any());
        }
        Ok(value.str()?.into_any())
    }
}
