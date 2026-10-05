//! A field's built-in validators, checked here in their order with their own messages - in place of
//! `field.validate()` for a field whose validators are all of these types and keep their default
//! message.

use std::cmp::Ordering;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyFloat, PyInt, PyList, PyString, PyTuple};
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::options::Options;
use crate::python::references::PythonReferences;
use crate::python::{ffi, objects};

/// A bound of `MinValueValidator`/`MaxValueValidator`: an int or a float is compared here with a
/// value of exactly its own type, as Python compares them; anything else through Python.
enum Limit {
    Integer(i64, Py<PyAny>),
    Float(f64, Py<PyAny>),
    Object(Py<PyAny>),
}

impl Limit {
    fn new(bound: Bound<'_, PyAny>) -> Self {
        if bound.is_exact_instance_of::<PyInt>() {
            if let Some(integer) = ffi::get_i64(&bound) {
                return Limit::Integer(integer, bound.unbind());
            }
        }
        if let Ok(float) = bound.cast_exact::<PyFloat>() {
            return Limit::Float(float.value(), bound.unbind());
        }
        Limit::Object(bound.unbind())
    }

    fn get_object(&self) -> &Py<PyAny> {
        match self {
            Limit::Integer(_, object) | Limit::Float(_, object) | Limit::Object(object) => object,
        }
    }

    /// `value < limit` (`is_below`) or `value > limit`, as Python answers it.
    fn compare(&self, value: &Bound<'_, PyAny>, is_below: bool) -> PyResult<bool> {
        let ordering = match self {
            Limit::Integer(limit, _) if value.is_exact_instance_of::<PyInt>() => {
                ffi::get_i64(value).map(|integer| integer.partial_cmp(limit))
            }
            Limit::Float(limit, _) => value.cast_exact::<PyFloat>().ok().map(|float| float.value().partial_cmp(limit)),
            _ => None,
        };
        if let Some(ordering) = ordering {
            return Ok(ordering == Some(if is_below { Ordering::Less } else { Ordering::Greater }));
        }
        let limit = self.get_object().bind(value.py());
        if is_below {
            value.lt(limit)
        } else {
            value.gt(limit)
        }
    }
}

/// One validator.
enum InlineCheck {
    /// `MinValueValidator`.
    MinValue(Limit),
    /// `MaxValueValidator`.
    MaxValue(Limit),
    /// `MinLengthValidator`.
    MinLength(usize),
    /// `MaxLengthValidator`.
    MaxLength(usize),
    /// `MaxDigitsValidator` of a value already quantized to `decimal_places` - only its digit count
    /// can fail.
    MaxDigits { max_digits: i64, decimal_places: i64 },
}

/// The `ValidationError` `Field.validate()` raises for a validator's `message`: `"<field>: <message>"`,
/// caused by the validator's own error.
pub fn validation_error(field_name: &Bound<'_, PyString>, message: &str) -> PyResult<PyErr> {
    let error_type = objects::validation_error_type(field_name.py())?;
    let cause = error_type.call1((message,))?;
    let error = error_type.call1((format!("{field_name}: {message}"),))?;
    error.setattr(intern!(field_name.py(), "__cause__"), cause)?;
    Ok(PyErr::from_value(error))
}

pub struct InlineChecks {
    checks: Vec<InlineCheck>,
}

impl InlineChecks {
    /// The checks of the `checks` option: `(type, bound)` pairs in validator order - types
    /// `min_value`, `max_value`, `min_length`, `max_length`, and `max_digits` with a
    /// `(max_digits, decimal_places)` bound.
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        let Some(items) = options.get_optional_object("checks")? else {
            return Ok(InlineChecks { checks: Vec::new() });
        };
        let items = items.into_bound(options.py()).cast_into::<PyList>()?;
        let mut checks = Vec::with_capacity(items.len());
        for item in items.iter() {
            let item = item.cast_into::<PyTuple>()?;
            let check_type: String = item.get_item(0)?.extract()?;
            let bound = item.get_item(1)?;
            checks.push(match check_type.as_str() {
                "min_value" => InlineCheck::MinValue(Limit::new(bound)),
                "max_value" => InlineCheck::MaxValue(Limit::new(bound)),
                "min_length" => InlineCheck::MinLength(bound.extract()?),
                "max_length" => InlineCheck::MaxLength(bound.extract()?),
                "max_digits" => {
                    let (max_digits, decimal_places) = bound.extract()?;
                    InlineCheck::MaxDigits { max_digits, decimal_places }
                }
                other => return Err(options.error(&format!("unknown check {other:?}"))),
            });
        }
        Ok(InlineChecks { checks })
    }

    /// The message of the first check `value` fails, None when it passes them all.
    fn get_failure(&self, value: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
        let py = value.py();
        for check in &self.checks {
            let failure = match check {
                InlineCheck::MinValue(min_value) => min_value
                    .compare(value, true)?
                    .then(|| format!("Value should be greater or equal to {}", min_value.get_object().bind(py))),
                InlineCheck::MaxValue(max_value) => max_value
                    .compare(value, false)?
                    .then(|| format!("Value should be less or equal to {}", max_value.get_object().bind(py))),
                InlineCheck::MinLength(min_length) => {
                    let length = value.len()?;
                    (length < *min_length).then(|| format!("Length of '{value}' {length} < {min_length}"))
                }
                InlineCheck::MaxLength(max_length) => {
                    let length = value.len()?;
                    (length > *max_length).then(|| format!("Length of '{value}' {length} > {max_length}"))
                }
                InlineCheck::MaxDigits { max_digits, decimal_places } => {
                    let adjusted: i64 = value.call_method0(intern!(py, "adjusted"))?.extract()?;
                    let digits = adjusted.max(-1) + 1 + decimal_places;
                    (digits > *max_digits)
                        .then(|| format!("Value '{value}' has {digits} digits, more than max_digits={max_digits}"))
                }
            };
            if failure.is_some() {
                return Ok(failure);
            }
        }
        Ok(None)
    }

    /// Raises the error `field.validate()` raises for `value`.
    pub fn check(&self, value: &Bound<'_, PyAny>, field_name: &Bound<'_, PyString>) -> PyResult<()> {
        match self.get_failure(value)? {
            Some(message) => Err(validation_error(field_name, &message)?),
            None => Ok(()),
        }
    }
}

impl PythonReferences for InlineChecks {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        for check in &self.checks {
            if let InlineCheck::MinValue(limit) | InlineCheck::MaxValue(limit) = check {
                visit.call(limit.get_object())?;
            }
        }
        Ok(())
    }
}
