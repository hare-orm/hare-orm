//! Whether a stored `DecimalField` value holds more fractional or whole digits than its
//! `DECIMAL(max_digits, decimal_places)` does - the check a column change runs over every row. A
//! value in a form this doesn't read goes to the Python function.

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyFloat, PyInt, PyString};
use pyo3::{PyTraverseError, PyVisit};

use crate::python::ffi::get_i64;
use crate::sqlite_functions::decimal_number::DecimalNumber;

/// The digits `Decimal.normalize()` keeps in the default context; a longer number rounds there.
const NORMALIZE_PRECISION: usize = 28;

/// The largest exponent read here - far inside the default context's limits.
const LARGEST_EXPONENT: i64 = 100_000;

/// What a value reads as.
enum DecimalValue {
    Number(DecimalNumber),
    /// NULL, a BLOB or NaN - never an overflow.
    NotNumber,
    Infinite,
}

/// The value of an argument, as `Decimal(get_decimal_text(value))` reads it; None for one this
/// leaves to Python.
fn read_value(value: &Bound<'_, PyAny>) -> Option<DecimalValue> {
    if value.is_none() || value.is_instance_of::<PyBytes>() {
        return Some(DecimalValue::NotNumber);
    }
    if let Ok(float) = value.cast::<PyFloat>() {
        let number = float.value();
        if number.is_nan() {
            return Some(DecimalValue::NotNumber);
        }
        if number.is_infinite() {
            return Some(DecimalValue::Infinite);
        }
        let mut buffer = ryu::Buffer::new();
        return DecimalNumber::parse(buffer.format_finite(number)).map(DecimalValue::Number);
    }
    if value.is_instance_of::<PyInt>() {
        let mut buffer = itoa::Buffer::new();
        return DecimalNumber::parse(buffer.format(get_i64(value)?)).map(DecimalValue::Number);
    }
    let text = value.cast::<PyString>().ok()?.to_str().ok()?;
    DecimalNumber::parse(text).map(DecimalValue::Number)
}

/// The overflow function; arguments it doesn't handle go to the Python one given.
#[pyclass(frozen, module = "rust.native.sqlite_functions")]
pub struct DecimalOverflow {
    /// `(value, max_digits, decimal_places)`, the Python function.
    overflows_in_python: Py<PyAny>,
}

#[pymethods]
impl DecimalOverflow {
    #[new]
    fn new(overflows_in_python: Py<PyAny>) -> Self {
        DecimalOverflow { overflows_in_python }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.overflows_in_python)
    }

    /// 1 when the value is a number with more fractional or whole digits than the type holds, 0
    /// otherwise - None, a BLOB and text that isn't a number included.
    fn overflows<'py>(
        &self,
        value: &Bound<'py, PyAny>,
        max_digits: &Bound<'py, PyAny>,
        decimal_places: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        let limits = (max_digits.extract::<i64>().ok())
            .zip(decimal_places.extract::<i64>().ok())
            .filter(|(digits, places)| digits.abs() <= LARGEST_EXPONENT && places.abs() <= LARGEST_EXPONENT);
        if let (Some(read), Some((max_digits_number, decimal_places_number))) = (read_value(value), limits) {
            let overflows = match read {
                DecimalValue::NotNumber => Some(false),
                DecimalValue::Infinite => Some(true),
                DecimalValue::Number(DecimalNumber::Zero) => Some(decimal_places_number < 0),
                DecimalValue::Number(DecimalNumber::Finite { digits, exponent, .. }) => {
                    (digits.len() <= NORMALIZE_PRECISION && exponent.abs() <= LARGEST_EXPONENT).then(|| {
                        let last_exponent = exponent - (digits.len() as i64 - 1);
                        let fractional_digits = (-last_exponent).max(0);
                        fractional_digits > decimal_places_number
                            || exponent >= max_digits_number - decimal_places_number
                    })
                }
            };
            if let Some(overflows) = overflows {
                return Ok(i64::from(overflows).into_pyobject(py)?.into_any());
            }
        }
        self.overflows_in_python.bind(py).call1((value, max_digits, decimal_places))
    }
}
