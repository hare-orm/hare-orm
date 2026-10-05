//! Values written into a JSON object built on SQLite as Postgres's jsonb holds them - a double in
//! positional notation, a timestamp and a time as Postgres writes them. A value in another form goes
//! to the Python function.

use std::fmt::Write;

use chrono::{Datelike, Timelike};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyFloat, PyInt, PyString};
use pyo3::{PyTraverseError, PyVisit};

use crate::sqlite_functions::decimal_number::DecimalNumber;
use crate::sqlite_functions::iso_moment::IsoMoment;
use crate::sqlite_functions::time_order::read_time;

/// The fraction of a second as Postgres writes it - without trailing zeros, none for zero.
fn get_fraction_text(microsecond: u32) -> String {
    if microsecond == 0 {
        return String::new();
    }
    let text = format!(".{microsecond:06}");
    text.trim_end_matches('0').to_owned()
}

/// A finite double other than zero in positional notation, an integral one without a fraction.
fn format_positional(number: f64) -> Option<String> {
    let mut buffer = ryu::Buffer::new();
    Some(DecimalNumber::parse(buffer.format_finite(number))?.format_positional())
}

/// The JSON value functions; values they don't write themselves go to the Python class given.
#[pyclass(frozen, module = "rust.native.sqlite_functions")]
pub struct JsonValues {
    /// `SqliteJsonValues`, its methods named as these.
    python_functions: Py<PyAny>,
}

#[pymethods]
impl JsonValues {
    #[new]
    fn new(python_functions: Py<PyAny>) -> Self {
        JsonValues { python_functions }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.python_functions)
    }

    /// A double as JSON text: a number in positional notation, `NaN`/`Infinity` as strings.
    #[expect(clippy::cast_precision_loss, reason = "an integer within 2**53 converts exactly")]
    fn format_float<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() {
            return Ok(py.None().into_bound(py));
        }
        let number = if let Ok(float) = value.cast::<PyFloat>() {
            Some(float.value())
        } else if value.is_instance_of::<PyInt>() {
            value
                .extract::<i64>()
                .ok()
                .filter(|integer| integer.unsigned_abs() <= 1 << 53)
                .map(|integer| integer as f64)
        } else {
            None
        };
        let text = number.and_then(|number| {
            if number.is_nan() {
                Some("\"NaN\"".to_owned())
            } else if number.is_infinite() {
                Some(if number > 0.0 { "\"Infinity\"" } else { "\"-Infinity\"" }.to_owned())
            } else if number == 0.0 {
                Some("0".to_owned())
            } else {
                format_positional(number)
            }
        });
        match text {
            Some(text) => Ok(PyString::new(py, &text).into_any()),
            None => self.python_functions.bind(py).call_method1(intern!(py, "format_float"), (value,)),
        }
    }

    /// A stored timestamp as Postgres writes it in JSON - an aware one in UTC, a naive one as its
    /// wall clock.
    fn format_timestamp<'py>(
        &self,
        value: &Bound<'py, PyAny>,
        is_aware: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() {
            return Ok(py.None().into_bound(py));
        }
        let moment = value.cast::<PyString>().ok().and_then(|text| IsoMoment::parse(text.to_str().ok()?));
        let aware = is_aware.is_truthy()?;
        if let Some(moment) = moment {
            // A naive text with an offset converts to the machine's local time - Python's.
            if aware || moment.offset_seconds.is_none() {
                let utc = moment.moment - chrono::Duration::seconds(i64::from(moment.offset_seconds.unwrap_or(0)));
                let shown = if aware { utc } else { moment.moment };
                // Python overflows past year 9999 and pads years below 1000 as the platform does.
                if !(1000..=9999).contains(&shown.year()) {
                    return self
                        .python_functions
                        .bind(py)
                        .call_method1(intern!(py, "format_timestamp"), (value, is_aware));
                }
                let text = format!(
                    "{}{}{}",
                    shown.format("%Y-%m-%dT%H:%M:%S"),
                    get_fraction_text(shown.nanosecond() / 1000),
                    if aware { "+00:00" } else { "" }
                );
                return Ok(PyString::new(py, &text).into_any());
            }
        }
        self.python_functions.bind(py).call_method1(intern!(py, "format_timestamp"), (value, is_aware))
    }

    /// A stored time as Postgres writes a `timetz` in JSON - the offset's minutes only when not zero.
    fn format_time<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() {
            return Ok(py.None().into_bound(py));
        }
        let parsed = value.cast::<PyString>().ok().and_then(|text| read_time(text.to_str().ok()?));
        let Some((wall_clock, offset)) = parsed else {
            return self.python_functions.bind(py).call_method1(intern!(py, "format_time"), (value,));
        };
        let seconds = wall_clock / 1_000_000;
        let microsecond = u32::try_from(wall_clock % 1_000_000).unwrap_or(0);
        let offset_minutes = offset.abs() / 60_000_000;
        let mut text = format!(
            "{:02}:{:02}:{:02}{}{}{:02}",
            seconds / 3600,
            seconds % 3600 / 60,
            seconds % 60,
            get_fraction_text(microsecond),
            if offset < 0 { '-' } else { '+' },
            offset_minutes / 60
        );
        if offset_minutes % 60 != 0 {
            let _ = write!(text, ":{:02}", offset_minutes % 60);
        }
        Ok(PyString::new(py, &text).into_any())
    }
}
