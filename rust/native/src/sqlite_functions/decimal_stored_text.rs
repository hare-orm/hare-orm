//! The text a `DecimalField` column of a row an UPDATE sets from an expression stores - the value
//! quantized to the field's places, as a plain write of it stores. A value in a form this doesn't
//! read goes to the Python function.

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyFloat, PyInt, PyString};
use pyo3::{PyTraverseError, PyVisit};

use crate::python::ffi::get_i64;
use crate::sqlite_functions::decimal_number::{DecimalNumber, Rounding};

/// The largest exponent or count of places read here - far inside the default context's limits.
const LARGEST_EXPONENT: i64 = 100_000;

/// The number of a value as `Decimal(value)` reads it; None for one this leaves to Python.
fn read_number(value: &Bound<'_, PyAny>) -> Option<DecimalNumber> {
    let number = if let Ok(float) = value.cast::<PyFloat>() {
        let number = float.value();
        if !number.is_finite() {
            return None;
        }
        DecimalNumber::from_float(number)
    } else if value.is_instance_of::<PyInt>() {
        let mut buffer = itoa::Buffer::new();
        DecimalNumber::parse(buffer.format(get_i64(value)?))?
    } else {
        DecimalNumber::parse(value.cast::<PyString>().ok()?.to_str().ok()?)?
    };
    match number {
        DecimalNumber::Finite { exponent, .. } if exponent.abs() > LARGEST_EXPONENT => None,
        number => Some(number),
    }
}

/// What a value is stored as.
enum StoredText {
    Text(String),
    /// A number that doesn't quantize, stored as it is.
    Unquantized,
    /// A value the Python function decides on.
    LeftToPython,
}

/// The stored-text function; arguments it doesn't handle go to the Python one given.
#[pyclass(frozen, module = "rust.native.sqlite_functions")]
pub struct DecimalStoredText {
    /// `(value, max_digits, decimal_places)`, the Python function.
    get_stored_text_in_python: Py<PyAny>,
    /// The least precision of the context the value is quantized in.
    min_precision: i64,
    /// The largest exponent decimal text is written in fixed-point notation for.
    max_fixed_point_exponent: i64,
}

#[pymethods]
impl DecimalStoredText {
    #[new]
    fn new(get_stored_text_in_python: Py<PyAny>, min_precision: i64, max_fixed_point_exponent: i64) -> Self {
        DecimalStoredText { get_stored_text_in_python, min_precision, max_fixed_point_exponent }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.get_stored_text_in_python)
    }

    /// The value quantized to `decimal_places` as bound text; the value itself when it isn't a
    /// number that quantizes - the written value's check rejects it.
    fn get_stored_text<'py>(
        &self,
        value: &Bound<'py, PyAny>,
        max_digits: &Bound<'py, PyAny>,
        decimal_places: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() || value.is_instance_of::<PyBytes>() {
            return Ok(value.clone());
        }
        match self.get_text(value, max_digits, decimal_places) {
            StoredText::Text(text) => Ok(PyString::new(py, &text).into_any()),
            StoredText::Unquantized => Ok(value.clone()),
            StoredText::LeftToPython => {
                self.get_stored_text_in_python.bind(py).call1((value, max_digits, decimal_places))
            }
        }
    }
}

impl DecimalStoredText {
    /// What a value is stored as.
    fn get_text(
        &self,
        value: &Bound<'_, PyAny>,
        max_digits: &Bound<'_, PyAny>,
        decimal_places: &Bound<'_, PyAny>,
    ) -> StoredText {
        let max_digits = max_digits.extract::<i64>().ok().filter(|digits| *digits <= LARGEST_EXPONENT);
        let decimal_places =
            decimal_places.extract::<i64>().ok().filter(|places| (0..=LARGEST_EXPONENT).contains(places));
        let (Some(max_digits), Some(decimal_places), Some(number)) = (max_digits, decimal_places, read_number(value))
        else {
            return StoredText::LeftToPython;
        };
        let Ok(precision) = usize::try_from(max_digits.max(self.min_precision)) else {
            return StoredText::LeftToPython;
        };
        let Some(scaled) = number.round_to_scale(decimal_places, Rounding::HalfEven, precision) else {
            return StoredText::Unquantized;
        };
        // Past these exponents the text is scientific, as `str()` writes it.
        if scaled.get_adjusted_exponent() > self.max_fixed_point_exponent
            || decimal_places > self.max_fixed_point_exponent
        {
            return StoredText::LeftToPython;
        }
        StoredText::Text(scaled.format_fixed())
    }
}
