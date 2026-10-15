//! Date and datetime shift and difference arithmetic on SQLite, over the text hare stores: dates and
//! aware datetimes here; a naive datetime (the machine's local time) or another form goes to the
//! Python functions.

use chrono::{Datelike, Duration, NaiveDateTime, NaiveTime};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::PyString;
use pyo3::{PyTraverseError, PyVisit};

use crate::sqlite_functions::iso_moment::{format_date, format_moment, IsoMoment};

const MICROSECONDS_PER_DAY: i64 = 86_400_000_000;

/// The moment of a str argument, None for anything else.
fn read_moment(value: &Bound<'_, PyAny>) -> Option<IsoMoment> {
    IsoMoment::parse(value.cast::<PyString>().ok()?.to_str().ok()?)
}

/// The UTC instant of an aware moment; None for a naive one.
fn get_utc(moment: &IsoMoment) -> Option<NaiveDateTime> {
    if moment.is_date {
        return None;
    }
    Some(moment.moment - Duration::seconds(i64::from(moment.offset_seconds?)))
}

/// A moment within the years `datetime` holds.
fn is_in_range(moment: NaiveDateTime) -> bool {
    (1..=9999).contains(&moment.year())
}

/// The temporal arithmetic functions; arguments they don't handle go to the Python class given.
#[pyclass(frozen, module = "rust.native.sqlite_functions")]
pub struct TemporalArithmetic {
    /// `TemporalArithmeticFunctions`, its methods named as these.
    python_functions: Py<PyAny>,
}

#[pymethods]
impl TemporalArithmetic {
    #[new]
    fn new(python_functions: Py<PyAny>) -> Self {
        TemporalArithmetic { python_functions }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.python_functions)
    }

    /// An aware datetime moved by `sign * microseconds`, as UTC text; None when either is NULL.
    fn shift_datetime<'py>(
        &self,
        value: &Bound<'py, PyAny>,
        microseconds: &Bound<'py, PyAny>,
        sign: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() || microseconds.is_none() {
            return Ok(py.None().into_bound(py));
        }
        let moment = read_moment(value);
        if let (Some(moment), Some(utc), Ok(microseconds), Ok(sign)) =
            (moment.as_ref(), moment.as_ref().and_then(get_utc), microseconds.extract::<i64>(), sign.extract::<i64>())
        {
            let delta = sign.checked_mul(microseconds).map(Duration::microseconds);
            // The value moves in its own offset first - out of the datetime range there, Python raises.
            let local = delta.and_then(|delta| moment.moment.checked_add_signed(delta));
            let shifted = delta.and_then(|delta| utc.checked_add_signed(delta));
            if let (Some(local), Some(shifted)) = (local, shifted) {
                if is_in_range(local) && is_in_range(shifted) {
                    return Ok(PyString::new(py, &format_moment(shifted, Some(0))).into_any());
                }
            }
        }
        self.python_functions.bind(py).call_method1(intern!(py, "shift_datetime"), (value, microseconds, sign))
    }

    /// A date (a datetime's date), taken as midnight, moved by `sign * microseconds` and floored to
    /// a date; None when either is NULL.
    fn shift_date<'py>(
        &self,
        value: &Bound<'py, PyAny>,
        microseconds: &Bound<'py, PyAny>,
        sign: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() || microseconds.is_none() {
            return Ok(py.None().into_bound(py));
        }
        if let (Some(moment), Ok(microseconds), Ok(sign)) =
            (read_moment(value), microseconds.extract::<i64>(), sign.extract::<i64>())
        {
            let midnight = moment.moment.date().and_time(NaiveTime::MIN);
            let shifted = sign
                .checked_mul(microseconds)
                .and_then(|delta| midnight.checked_add_signed(Duration::microseconds(delta)));
            if let Some(shifted) = shifted.filter(|shifted| is_in_range(*shifted)) {
                return Ok(PyString::new(py, &format_date(shifted.date())).into_any());
            }
        }
        self.python_functions.bind(py).call_method1(intern!(py, "shift_date"), (value, microseconds, sign))
    }

    /// `left - right` of two aware datetimes in whole microseconds; None when either is NULL.
    fn difference_datetime<'py>(
        &self,
        left: &Bound<'py, PyAny>,
        right: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = left.py();
        if left.is_none() || right.is_none() {
            return Ok(py.None().into_bound(py));
        }
        if let (Some(left_utc), Some(right_utc)) =
            (read_moment(left).as_ref().and_then(get_utc), read_moment(right).as_ref().and_then(get_utc))
        {
            if let Some(microseconds) = (left_utc - right_utc).num_microseconds() {
                return Ok(microseconds.into_pyobject(py)?.into_any());
            }
        }
        self.python_functions.bind(py).call_method1(intern!(py, "difference_datetime"), (left, right))
    }

    /// `left - right` of two dates (a datetime's date) in whole microseconds; None when either is NULL.
    fn difference_date<'py>(&self, left: &Bound<'py, PyAny>, right: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = left.py();
        if left.is_none() || right.is_none() {
            return Ok(py.None().into_bound(py));
        }
        if let (Some(left_moment), Some(right_moment)) = (read_moment(left), read_moment(right)) {
            let days = (left_moment.moment.date() - right_moment.moment.date()).num_days();
            return Ok((days * MICROSECONDS_PER_DAY).into_pyobject(py)?.into_any());
        }
        self.python_functions.bind(py).call_method1(intern!(py, "difference_date"), (left, right))
    }
}
