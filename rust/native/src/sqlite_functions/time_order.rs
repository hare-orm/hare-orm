//! The order of `TimeField` text on SQLite, as Postgres orders `TIMETZ`: by the UTC time (the wall
//! clock minus its offset, not wrapped around midnight), then by the offset; text that isn't a time
//! after every time, by its own text - the time collation and its sort key.

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyString};
use pyo3::{PyTraverseError, PyVisit};

use crate::sqlite_functions::decimal_number::biased;
use crate::sqlite_functions::sqlite_value_key::{SqliteValue, BLOB_CLASS, NUMBER_CLASS, TEXT_CLASS};

const MICROSECONDS_PER_SECOND: i64 = 1_000_000;

/// The time collation's functions; a text they don't read themselves (another ISO form, a time
/// that isn't one) goes to the Python ones given.
#[pyclass(frozen, module = "rust.native.sqlite_functions")]
pub struct TimeOrder {
    /// `(value) -> bytes | None`, the key of any value.
    fallback_sort_key: Py<PyAny>,
    /// `(left, right) -> int`, the collation's comparison.
    fallback_compare: Py<PyAny>,
}

/// Two ASCII digits at `position` as a number below `limit`.
fn read_two_digits(bytes: &[u8], position: usize, limit: i64) -> Option<i64> {
    let tens = bytes.get(position).filter(|byte| byte.is_ascii_digit())?;
    let ones = bytes.get(position + 1).filter(|byte| byte.is_ascii_digit())?;
    let value = i64::from(tens - b'0') * 10 + i64::from(ones - b'0');
    (value < limit).then_some(value)
}

/// The wall clock and the offset, in microseconds, of a time text in the form
/// `HH:MM[:SS[.f{1,6}]][(+|-)HH:MM]`; None for any other text.
pub fn read_time(text: &str) -> Option<(i64, i64)> {
    let bytes = text.as_bytes();
    let hour = read_two_digits(bytes, 0, 24)?;
    if bytes.get(2) != Some(&b':') {
        return None;
    }
    let minute = read_two_digits(bytes, 3, 60)?;
    let mut position = 5;
    let mut second = 0;
    let mut microsecond = 0;
    if bytes.get(position) == Some(&b':') {
        second = read_two_digits(bytes, position + 1, 60)?;
        position += 3;
        if bytes.get(position) == Some(&b'.') {
            position += 1;
            let start = position;
            while bytes.get(position).is_some_and(u8::is_ascii_digit) {
                position += 1;
            }
            let length = position - start;
            if !(1..=6).contains(&length) {
                return None;
            }
            microsecond = text[start..position].parse::<i64>().ok()? * 10_i64.pow((6 - length) as u32);
        }
    }
    let wall_clock = ((hour * 60 + minute) * 60 + second) * MICROSECONDS_PER_SECOND + microsecond;
    let offset = match bytes.get(position) {
        None => 0,
        Some(&sign @ (b'+' | b'-')) => {
            let offset_hour = read_two_digits(bytes, position + 1, 24)?;
            if bytes.get(position + 3) != Some(&b':') || bytes.len() != position + 6 {
                return None;
            }
            let offset_minute = read_two_digits(bytes, position + 4, 60)?;
            let offset = (offset_hour * 60 + offset_minute) * 60 * MICROSECONDS_PER_SECOND;
            if sign == b'-' {
                -offset
            } else {
                offset
            }
        }
        Some(_) => return None,
    };
    Some((wall_clock, offset))
}

/// The key of a TEXT value that is a time in the form `read_time()` reads.
fn get_text_key(text: &str) -> Option<Vec<u8>> {
    let (wall_clock, offset) = read_time(text)?;
    let mut key = Vec::with_capacity(18);
    key.extend_from_slice(&[TEXT_CLASS, 0x01]);
    key.extend_from_slice(&biased(wall_clock - offset).to_be_bytes());
    key.extend_from_slice(&biased(-offset).to_be_bytes());
    Some(key)
}

#[pymethods]
impl TimeOrder {
    #[new]
    fn new(fallback_sort_key: Py<PyAny>, fallback_compare: Py<PyAny>) -> Self {
        TimeOrder { fallback_sort_key, fallback_compare }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback_sort_key)?;
        visit.call(&self.fallback_compare)
    }

    /// The byte key `ORDER BY` sorts a time column's value by, as the collation orders it.
    fn sort_key<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        let key = match SqliteValue::read(value)? {
            Some(SqliteValue::Null) => return Ok(py.None().into_bound(py)),
            Some(SqliteValue::Number(number)) => {
                let mut key = vec![NUMBER_CLASS];
                number.write_key(&mut key);
                Some(key)
            }
            Some(SqliteValue::Text(text)) => get_text_key(text),
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
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_the_stored_forms() {
        assert_eq!(read_time("10:30"), Some((37_800_000_000, 0)));
        assert_eq!(read_time("10:30:05.25"), Some((37_805_250_000, 0)));
        assert_eq!(read_time("10:30:00-02:30"), Some((37_800_000_000, -9_000_000_000)));
        for text in ["24:00", "10:60", "10:30:00.1234567", "10:30Z", "10:30:00+0300", "1030", "T10:30", "10:30 "] {
            assert_eq!(read_time(text), None, "{text}");
        }
    }
}
