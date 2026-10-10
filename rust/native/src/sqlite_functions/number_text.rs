//! The text a number is concatenated as on SQLite - Postgres's own `::text` output: a double at
//! its shortest round-tripping digits, a decimal rounded half away from zero to its scale. A value
//! in a form this doesn't read goes to the Python function.

use pyo3::prelude::*;
use pyo3::types::{PyFloat, PyInt, PyString};
use pyo3::{PyTraverseError, PyVisit};

use crate::python::ffi::get_i64;
use crate::sqlite_functions::decimal_number::{DecimalNumber, Rounding};

/// The largest scale read here.
const LARGEST_SCALE: i64 = 100_000;

/// The decimal exponents a double is written in positional notation for, as Postgres writes it.
const POSITIONAL_EXPONENTS: std::ops::Range<i64> = -4..15;

/// A double as Postgres writes it: positional for a decimal exponent from -4 to 14, else scientific
/// with an exponent of at least two digits.
fn format_float(number: f64) -> String {
    if number.is_nan() {
        return "NaN".to_owned();
    }
    if number.is_infinite() {
        return if number > 0.0 { "Infinity" } else { "-Infinity" }.to_owned();
    }
    let mut buffer = ryu::Buffer::new();
    let shortest = DecimalNumber::parse(buffer.format_finite(number)).expect("ryu writes a plain number");
    let DecimalNumber::Finite { negative, digits, exponent } = &shortest else {
        return if number.is_sign_negative() { "-0" } else { "0" }.to_owned();
    };
    if POSITIONAL_EXPONENTS.contains(exponent) {
        return shortest.format_positional();
    }
    let digits = std::str::from_utf8(digits).expect("ASCII digits");
    let mantissa = if digits.len() > 1 { format!("{}.{}", &digits[..1], &digits[1..]) } else { digits.to_owned() };
    let exponent_sign = if *exponent < 0 { '-' } else { '+' };
    format!("{}{mantissa}e{exponent_sign}{:02}", if *negative { "-" } else { "" }, exponent.abs())
}

/// The double of an argument, as `float(value)` gives it; None for one this leaves to Python.
fn read_float(value: &Bound<'_, PyAny>) -> Option<f64> {
    if let Ok(float) = value.cast::<PyFloat>() {
        return Some(float.value());
    }
    // An int of 64 bits rounds to the nearest double, a tie to even, as `float()` rounds it.
    #[expect(clippy::cast_precision_loss, reason = "the same rounding as float()")]
    value.is_instance_of::<PyInt>().then(|| get_i64(value)).flatten().map(|integer| integer as f64)
}

/// The decimal of an argument - a double at its shortest round-tripping digits - as
/// `format_decimal()` reads it; None for one this leaves to Python.
fn read_decimal(value: &Bound<'_, PyAny>) -> Option<DecimalNumber> {
    if let Ok(float) = value.cast::<PyFloat>() {
        let number = float.value();
        let mut buffer = ryu::Buffer::new();
        return number.is_finite().then(|| DecimalNumber::parse(buffer.format_finite(number))).flatten();
    }
    if value.is_instance_of::<PyInt>() {
        let mut buffer = itoa::Buffer::new();
        return DecimalNumber::parse(buffer.format(get_i64(value)?));
    }
    DecimalNumber::parse(value.cast::<PyString>().ok()?.to_str().ok()?)
}

/// The number text function; arguments it doesn't handle go to the Python one given.
#[pyclass(frozen, module = "rust.native.sqlite_functions")]
pub struct NumberText {
    /// `(value, scale)`, the Python function.
    format_number_in_python: Py<PyAny>,
    /// The precision of the context a decimal is rounded in.
    precision: usize,
}

#[pymethods]
impl NumberText {
    #[new]
    fn new(format_number_in_python: Py<PyAny>, precision: usize) -> Self {
        NumberText { format_number_in_python, precision }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.format_number_in_python)
    }

    /// The text of a number - a double for no scale, a decimal rounded to `scale` places; None for
    /// None.
    fn format_number<'py>(&self, value: &Bound<'py, PyAny>, scale: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() {
            return Ok(py.None().into_bound(py));
        }
        let text = if scale.is_none() {
            read_float(value).map(format_float)
        } else {
            scale
                .extract::<i64>()
                .ok()
                .filter(|scale| (0..=LARGEST_SCALE).contains(scale))
                .zip(read_decimal(value))
                .and_then(|(scale, number)| number.round_to_scale(scale, Rounding::HalfUp, self.precision))
                .map(|scaled| scaled.format_fixed())
        };
        match text {
            Some(text) => Ok(PyString::new(py, &text).into_any()),
            None => self.format_number_in_python.bind(py).call1((value, scale)),
        }
    }
}
