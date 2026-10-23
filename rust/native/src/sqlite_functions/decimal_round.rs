//! `Round()` of a Decimal on SQLite, which keeps it as text - rounded exactly half away from zero to
//! its places. A value in a form this doesn't read goes to the Python function.

use pyo3::prelude::*;
use pyo3::types::{PyFloat, PyInt, PyString};
use pyo3::{PyTraverseError, PyVisit};

use crate::python::ffi::get_i64;
use crate::sqlite_functions::decimal_number::DecimalNumber;

/// The largest scale read here.
const LARGEST_SCALE: i64 = 100_000;

/// The decimal of an argument - a double at its shortest round-tripping digits - as
/// `round_decimal()` reads it; None for one this leaves to Python.
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

/// The decimal rounding function; arguments it doesn't handle go to the Python one given.
#[pyclass(frozen, module = "rust.native.sqlite_functions")]
pub struct DecimalRound {
    /// `(value, scale)`, the Python function.
    round_decimal_in_python: Py<PyAny>,
    /// The precision of the context a decimal is rounded in.
    precision: usize,
}

#[pymethods]
impl DecimalRound {
    #[new]
    fn new(round_decimal_in_python: Py<PyAny>, precision: usize) -> Self {
        DecimalRound { round_decimal_in_python, precision }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.round_decimal_in_python)
    }

    /// The text of a decimal rounded to `scale` places; None for None.
    fn round_decimal<'py>(&self, value: &Bound<'py, PyAny>, scale: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() {
            return Ok(py.None().into_bound(py));
        }
        let text = scale
            .extract::<i64>()
            .ok()
            .filter(|scale| (0..=LARGEST_SCALE).contains(scale))
            .zip(read_decimal(value))
            .and_then(|(scale, number)| number.round_to_scale(scale, self.precision))
            .map(|scaled| scaled.format_fixed());
        match text {
            Some(text) => Ok(PyString::new(py, &text).into_any()),
            None => self.round_decimal_in_python.bind(py).call1((value, scale)),
        }
    }
}
