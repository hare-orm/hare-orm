//! The checks of the built-in validators - the one implementation of each, called by the validator itself
//! and, past the validator's Python call, by a column writer.

use pyo3::exceptions::PyValueError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyInt, PyString, PyTuple};
use pyo3::{PyTraverseError, PyVisit};

/// What a check compares.
enum Check {
    /// A number at least (`is_lower`) or at most `limit`: an int against an int limit within 64 bits is
    /// compared here, anything else by Python's comparison.
    Limit { limit: Py<PyAny>, limit_i64: Option<i64>, is_lower: bool },
    /// A length (`len()`) at least (`is_lower`) or at most `limit`.
    Length { limit: i64, is_lower: bool },
    /// A `Decimal` of at most `max_digits` digits, at most `decimal_places` of them after the point.
    Digits { max_digits: i64, decimal_places: i64 },
    /// A value the compiled pattern's `match`, `fullmatch` or `search` (`pattern_method`) finds a match in;
    /// with `requires_text`, a value that isn't a `str` fails before it.
    Pattern { pattern_method: Py<PyAny>, requires_text: bool },
    /// A UUID whose `version` equals `version`.
    UuidVersion { version: Py<PyAny> },
}

/// A built-in validator's check: `failure(value)` is None for a value it passes, else what failed - True,
/// or for the digits of a `Decimal` `(reason, count)` with the reason `"not_finite"`, `"digits"`,
/// `"decimal_places"` or `"whole_digits"`. A value the check can't take (an int given to a length check)
/// raises what Python raises for it. A check pickles and copies as the call that made it.
#[pyclass(frozen, module = "rust.native.rows")]
pub struct ValueCheck {
    check: Check,
    /// The static method that made the check, and its arguments.
    constructor: &'static str,
    arguments: Py<PyTuple>,
}

impl ValueCheck {
    /// None when `value` passes, else what failed.
    pub fn get_failure<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
        let py = value.py();
        let failed = match &self.check {
            Check::Limit { limit, limit_i64, is_lower } => {
                let compared_here = match limit_i64 {
                    Some(limit_number) if value.is_exact_instance_of::<PyInt>() => value
                        .extract::<i64>()
                        .ok()
                        .map(|number| if *is_lower { number < *limit_number } else { number > *limit_number }),
                    _ => None,
                };
                match compared_here {
                    Some(failed) => failed,
                    None if *is_lower => value.lt(limit.bind(py))?,
                    None => value.gt(limit.bind(py))?,
                }
            }
            Check::Length { limit, is_lower } => {
                let length = value.len()? as i64;
                if *is_lower {
                    length < *limit
                } else {
                    length > *limit
                }
            }
            Check::Digits { max_digits, decimal_places } => {
                return Self::get_digits_failure(value, *max_digits, *decimal_places);
            }
            Check::Pattern { pattern_method, requires_text } => {
                (*requires_text && !value.is_instance_of::<PyString>())
                    || pattern_method.bind(py).call1((value,))?.is_none()
            }
            Check::UuidVersion { version } => value.getattr(intern!(py, "version"))?.ne(version.bind(py))?,
        };
        Ok(failed.then(|| pyo3::types::PyBool::new(py, true).to_owned().into_any()))
    }

    /// The failure of a `Decimal`'s digits - counted from `as_tuple()`: a positive exponent adds trailing
    /// zeros, a negative one past every digit adds leading zeros after the point.
    fn get_digits_failure<'py>(
        value: &Bound<'py, PyAny>,
        max_digits: i64,
        decimal_places: i64,
    ) -> PyResult<Option<Bound<'py, PyAny>>> {
        let py = value.py();
        let parts = value.call_method0(intern!(py, "as_tuple"))?;
        let digit_tuple = parts.get_item(1)?;
        let exponent = parts.get_item(2)?;
        let failure = |reason: &str, count: i64| -> PyResult<Option<Bound<'py, PyAny>>> {
            Ok(Some(
                PyTuple::new(py, [PyString::new(py, reason).into_any(), count.into_pyobject(py)?.into_any()])?
                    .into_any(),
            ))
        };
        if !exponent.is_exact_instance_of::<PyInt>() {
            return failure("not_finite", 0);
        }
        let exponent: i64 = exponent.extract()?;
        let digit_count = digit_tuple.len()? as i64;
        let (digits, decimals) = if exponent >= 0 {
            let is_zero = digit_count == 1 && digit_tuple.get_item(0)?.extract::<i64>()? == 0;
            (if is_zero { digit_count } else { digit_count + exponent }, 0)
        } else if -exponent > digit_count {
            (-exponent, -exponent)
        } else {
            (digit_count, -exponent)
        };
        if digits > max_digits {
            return failure("digits", digits);
        }
        if decimals > decimal_places {
            return failure("decimal_places", decimals);
        }
        let whole_digits = digits - decimals;
        if whole_digits > max_digits - decimal_places {
            return failure("whole_digits", whole_digits);
        }
        Ok(None)
    }
}

#[pymethods]
impl ValueCheck {
    /// A number at least `limit`.
    #[staticmethod]
    fn lower_limit(limit: Bound<'_, PyAny>) -> PyResult<Self> {
        let arguments = PyTuple::new(limit.py(), [&limit])?.unbind();
        Ok(ValueCheck { check: Self::make_limit(limit, true), constructor: "lower_limit", arguments })
    }

    /// A number at most `limit`.
    #[staticmethod]
    fn upper_limit(limit: Bound<'_, PyAny>) -> PyResult<Self> {
        let arguments = PyTuple::new(limit.py(), [&limit])?.unbind();
        Ok(ValueCheck { check: Self::make_limit(limit, false), constructor: "upper_limit", arguments })
    }

    /// A length at least `limit`.
    #[staticmethod]
    fn min_length(py: Python<'_>, limit: i64) -> PyResult<Self> {
        let arguments = PyTuple::new(py, [limit])?.unbind();
        Ok(ValueCheck { check: Check::Length { limit, is_lower: true }, constructor: "min_length", arguments })
    }

    /// A length at most `limit`.
    #[staticmethod]
    fn max_length(py: Python<'_>, limit: i64) -> PyResult<Self> {
        let arguments = PyTuple::new(py, [limit])?.unbind();
        Ok(ValueCheck { check: Check::Length { limit, is_lower: false }, constructor: "max_length", arguments })
    }

    /// A `Decimal` of at most `max_digits` digits, at most `decimal_places` of them after the point.
    #[staticmethod]
    fn max_digits(py: Python<'_>, max_digits: i64, decimal_places: i64) -> PyResult<Self> {
        let arguments = PyTuple::new(py, [max_digits, decimal_places])?.unbind();
        Ok(ValueCheck { check: Check::Digits { max_digits, decimal_places }, constructor: "max_digits", arguments })
    }

    /// A value the compiled `pattern`'s `method_name` (`match`, `fullmatch` or `search`) matches.
    #[staticmethod]
    fn pattern(pattern: Bound<'_, PyAny>, method_name: &str, requires_text: bool) -> PyResult<Self> {
        if !matches!(method_name, "match" | "fullmatch" | "search") {
            return Err(PyValueError::new_err(format!("no pattern method {method_name:?}")));
        }
        let py = pattern.py();
        let pattern_method = pattern.getattr(method_name)?.unbind();
        let arguments = PyTuple::new(
            py,
            [
                pattern.into_any(),
                PyString::new(py, method_name).into_any(),
                pyo3::types::PyBool::new(py, requires_text).to_owned().into_any(),
            ],
        )?
        .unbind();
        Ok(ValueCheck { check: Check::Pattern { pattern_method, requires_text }, constructor: "pattern", arguments })
    }

    /// A UUID of `version`.
    #[staticmethod]
    fn uuid_version(py: Python<'_>, version: Py<PyAny>) -> PyResult<Self> {
        let arguments = PyTuple::new(py, [version.bind(py)])?.unbind();
        Ok(ValueCheck { check: Check::UuidVersion { version }, constructor: "uuid_version", arguments })
    }

    /// None when `value` passes, else what failed.
    fn failure<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
        self.get_failure(value)
    }

    /// The call that makes this check again - `pickle` and `copy` use it.
    fn __reduce__<'py>(slf: &Bound<'py, Self>) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyTuple>)> {
        let check = slf.get();
        let constructor = slf.get_type().getattr(check.constructor)?;
        Ok((constructor, check.arguments.bind(slf.py()).clone()))
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.arguments)?;
        match &self.check {
            Check::Limit { limit, .. } => visit.call(limit),
            Check::Pattern { pattern_method, .. } => visit.call(pattern_method),
            Check::UuidVersion { version } => visit.call(version),
            Check::Length { .. } | Check::Digits { .. } => Ok(()),
        }
    }
}

impl ValueCheck {
    fn make_limit(limit: Bound<'_, PyAny>, is_lower: bool) -> Check {
        let limit_i64 = if limit.is_exact_instance_of::<PyInt>() { limit.extract::<i64>().ok() } else { None };
        Check::Limit { limit: limit.unbind(), limit_i64, is_lower }
    }
}
