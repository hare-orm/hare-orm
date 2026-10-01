//! `Cast()` on SQLite with Postgres's rules - the common conversions of numbers and number text to an
//! integer, a float, a decimal or text; any other goes to the Python function, which also raises
//! for bad input.

use pyo3::prelude::*;
use pyo3::types::{PyFloat, PyInt, PyString};
use pyo3::{PyTraverseError, PyVisit};

use crate::sqlite_functions::decimal_number::DecimalNumber;

/// The largest magnitude a double converts to an `i128` exactly from.
const DOUBLE_INTEGER_LIMIT: f64 = 1.7e38;

/// The most digits a rounded coefficient is written with here - a longer one goes to Python.
const MAXIMUM_COEFFICIENT_DIGITS: i64 = 60;

/// A number other than zero rounded half away from zero to `decimal_places`: its sign and its
/// coefficient's digits (the value in units of `10**-decimal_places`, no leading zeros, "0" for
/// zero); None for zero, whose sign Python keeps, or a coefficient past `MAXIMUM_COEFFICIENT_DIGITS`.
fn round_half_up(number: &DecimalNumber, decimal_places: u32) -> Option<(bool, String)> {
    let DecimalNumber::Finite { negative, digits, exponent } = number else {
        return None;
    };
    // How many of the digits fall at or above the last kept place.
    let kept = exponent + 1 + i64::from(decimal_places);
    if kept > MAXIMUM_COEFFICIENT_DIGITS {
        return None;
    }
    let (mut coefficient, rounds_up) = if kept <= 0 {
        (Vec::new(), kept == 0 && digits[0] >= b'5')
    } else {
        let kept = usize::try_from(kept).ok()?;
        let mut coefficient: Vec<u8> = digits.iter().take(kept).copied().collect();
        coefficient.resize(kept, b'0');
        (coefficient, digits.get(kept).is_some_and(|&digit| digit >= b'5'))
    };
    if rounds_up {
        let mut position = coefficient.len();
        loop {
            if position == 0 {
                coefficient.insert(0, b'1');
                break;
            }
            position -= 1;
            if coefficient[position] == b'9' {
                coefficient[position] = b'0';
            } else {
                coefficient[position] += 1;
                break;
            }
        }
    }
    let text = String::from_utf8(coefficient).expect("ASCII digits");
    let text = text.trim_start_matches('0');
    Some((*negative, if text.is_empty() { "0".to_owned() } else { text.to_owned() }))
}

/// The number of text in the form `\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?\s*` - ASCII spaces
/// only; None for any other text.
fn read_number_text(text: &str) -> Option<DecimalNumber> {
    if !text.is_ascii() {
        return None;
    }
    DecimalNumber::parse(text.trim_matches(|character: char| character.is_ascii_whitespace()))
}

/// The integer of text in the form `\s*[+-]?\d+\s*` - ASCII only; None for any other text.
fn read_integer_text(text: &str) -> Option<i128> {
    if !text.is_ascii() {
        return None;
    }
    let trimmed = text.trim_matches(|character: char| character.is_ascii_whitespace());
    let unsigned = trimmed.strip_prefix(['+', '-']).unwrap_or(trimmed);
    if unsigned.is_empty() || !unsigned.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    trimmed.parse().ok()
}

/// The Python `Cast()` function's fast paths; anything else goes to it.
#[pyclass(frozen, module = "rust.native.sqlite_functions")]
pub struct SqliteCast {
    cast_in_python: Py<PyAny>,
}

impl SqliteCast {
    /// The number a value converts from to a numeric type - None for a source or a form this
    /// leaves to Python (a float, a date, a boolean, text with other spaces or digits).
    fn read_number(value: &Bound<'_, PyAny>, source: &str) -> PyResult<Option<DecimalNumber>> {
        if matches!(source, "boolean" | "date" | "datetime" | "time") {
            return Ok(None);
        }
        if value.is_instance_of::<PyInt>() {
            let Ok(integer) = value.extract::<i128>() else {
                return Ok(None);
            };
            let digits = integer.unsigned_abs().to_string().into_bytes();
            return Ok(Some(DecimalNumber::from_digits(integer < 0, digits, 0)));
        }
        if let Ok(text) = value.cast::<PyString>() {
            return Ok(read_number_text(text.to_str()?));
        }
        Ok(None)
    }

    /// The integer a value casts to; None when Python decides.
    fn to_integer(value: &Bound<'_, PyAny>, source: &str, bits: u32) -> PyResult<Option<i128>> {
        if source == "boolean" || !(1..=64).contains(&bits) {
            return Ok(None);
        }
        let number = if let (Ok(text), "text" | "unknown") = (value.cast::<PyString>(), source) {
            read_integer_text(text.to_str()?)
        } else if let Ok(float) = value.cast::<PyFloat>() {
            let float = float.value();
            #[expect(clippy::cast_possible_truncation, reason = "a rounded double below 1.7e38 is an i128 exactly")]
            (float.is_finite() && float.abs() < DOUBLE_INTEGER_LIMIT).then(|| float.round_ties_even() as i128)
        } else if value.is_instance_of::<PyInt>() {
            value.extract::<i128>().ok()
        } else {
            match Self::read_number(value, source)? {
                Some(DecimalNumber::Zero) => Some(0),
                Some(number) => round_half_up(&number, 0).and_then(|(negative, digits)| {
                    digits.parse::<i128>().ok().map(|magnitude| if negative { -magnitude } else { magnitude })
                }),
                None => None,
            }
        };
        let limit = 1_i128 << (bits - 1);
        Ok(number.filter(|number| (-limit..limit).contains(number)))
    }

    /// The decimal text a value casts to; None when Python decides.
    fn to_decimal(
        value: &Bound<'_, PyAny>,
        source: &str,
        max_digits: i64,
        decimal_places: u32,
    ) -> PyResult<Option<String>> {
        // str() of a Decimal goes scientific for a value this small; Python writes it.
        if decimal_places > 6 {
            return Ok(None);
        }
        let Some((negative, digits)) =
            Self::read_number(value, source)?.and_then(|number| round_half_up(&number, decimal_places))
        else {
            return Ok(None);
        };
        let places = decimal_places as usize;
        let padded = format!("{digits:0>width$}", width = places + 1);
        let (whole, fraction) = padded.split_at(padded.len() - places);
        let whole_digits = if whole == "0" { 0 } else { whole.len() as i64 };
        if whole_digits > max_digits - i64::from(decimal_places) {
            return Ok(None);
        }
        let sign = if negative { "-" } else { "" };
        Ok(Some(if places == 0 { format!("{sign}{whole}") } else { format!("{sign}{whole}.{fraction}") }))
    }
}

#[pymethods]
impl SqliteCast {
    #[new]
    fn new(cast_in_python: Py<PyAny>) -> Self {
        SqliteCast { cast_in_python }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.cast_in_python)
    }

    /// `value` converted to `target` from a `source` type - see the Python function.
    fn cast<'py>(
        &self,
        value: &Bound<'py, PyAny>,
        target: &Bound<'py, PyAny>,
        source: &Bound<'py, PyAny>,
        first_parameter: &Bound<'py, PyAny>,
        second_parameter: &Bound<'py, PyAny>,
        is_aware: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() {
            return Ok(py.None().into_bound(py));
        }
        let target_text = target.cast::<PyString>().ok().and_then(|text| text.to_str().ok());
        let source_text = source.cast::<PyString>().ok().and_then(|text| text.to_str().ok());
        if let (Some(target_text), Some(source_text)) = (target_text, source_text) {
            match target_text {
                "integer" => {
                    if let Ok(bits) = first_parameter.extract::<u32>() {
                        if let Some(integer) = Self::to_integer(value, source_text, bits)? {
                            return Ok(integer.into_pyobject(py)?.into_any());
                        }
                    }
                }
                "float" if source_text != "boolean" => {
                    if let Ok(float) = value.cast::<PyFloat>() {
                        return Ok(float.clone().into_any());
                    }
                    if value.is_instance_of::<PyInt>() {
                        return Ok(PyFloat::new(py, value.extract::<f64>()?).into_any());
                    }
                }
                "decimal" => {
                    if let (Ok(max_digits), Ok(decimal_places)) =
                        (first_parameter.extract::<i64>(), second_parameter.extract::<u32>())
                    {
                        if let Some(text) = Self::to_decimal(value, source_text, max_digits, decimal_places)? {
                            return Ok(PyString::new(py, &text).into_any());
                        }
                    }
                }
                "text" if !matches!(source_text, "boolean" | "datetime" | "time") => {
                    let max_length =
                        if first_parameter.is_none() { None } else { first_parameter.extract::<usize>().ok() };
                    let text = if let Ok(text) = value.cast::<PyString>() {
                        Some(text.to_str()?.to_owned())
                    } else if value.is_instance_of::<PyInt>() {
                        Some(value.str()?.to_str()?.to_owned())
                    } else {
                        None
                    };
                    if let (Some(text), true) = (text, first_parameter.is_none() || max_length.is_some()) {
                        let cut: String = match max_length {
                            Some(length) => text.chars().take(length).collect(),
                            None => text,
                        };
                        return Ok(PyString::new(py, &cut).into_any());
                    }
                }
                _ => {}
            }
        }
        self.cast_in_python.bind(py).call1((value, target, source, first_parameter, second_parameter, is_aware))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rounded(text: &str, decimal_places: u32) -> (bool, String) {
        round_half_up(&DecimalNumber::parse(text).expect("a number"), decimal_places).expect("a rounded number")
    }

    #[test]
    fn rounds_half_away_from_zero() {
        assert_eq!(rounded("2.5", 0), (false, "3".to_owned()));
        assert_eq!(rounded("-2.5", 0), (true, "3".to_owned()));
        assert_eq!(rounded("2.4999", 0), (false, "2".to_owned()));
        assert_eq!(rounded("999.995", 2), (false, "100000".to_owned()));
        assert_eq!(rounded("0.004", 2), (false, "0".to_owned()));
        assert_eq!(rounded("-0.004", 2), (true, "0".to_owned()));
        assert_eq!(rounded("0.005", 2), (false, "1".to_owned()));
        assert_eq!(rounded("12e3", 1), (false, "120000".to_owned()));
        assert_eq!(round_half_up(&DecimalNumber::Zero, 3), None);
    }
}
