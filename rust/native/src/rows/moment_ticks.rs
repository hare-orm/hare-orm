//! The ticks since 1970 a column of moments stores, for every value of the column in one call.

use pyo3::exceptions::PyOverflowError;
use pyo3::prelude::*;
use pyo3::types::{PyDateTime, PyList};

use crate::rows::value_writing::count_microseconds;

const MICROSECOND_DIGITS: u32 = 6;

/// Each moment of `values` as the ticks since `epoch` of a column keeping `moment_scale` fractional
/// digits of a second - what is finer than a tick is dropped, rounding down; any other value (None,
/// ticks already) is kept as it is.
#[pyfunction]
pub fn get_moment_ticks<'py>(
    py: Python<'py>,
    values: &Bound<'py, PyAny>,
    epoch: &Bound<'py, PyAny>,
    moment_scale: u32,
) -> PyResult<Bound<'py, PyList>> {
    let overflow = || PyOverflowError::new_err("the ticks of a moment don't fit");
    let (multiplier, divisor) = if moment_scale > MICROSECOND_DIGITS {
        (10_i128.checked_pow(moment_scale - MICROSECOND_DIGITS).ok_or_else(overflow)?, 1)
    } else {
        (1, 10_i128.pow(MICROSECOND_DIGITS - moment_scale))
    };
    let ticks = PyList::empty(py);
    for value in values.try_iter()? {
        let value = value?;
        if !value.is_instance_of::<PyDateTime>() {
            ticks.append(value)?;
            continue;
        }
        let microseconds = count_microseconds(&value.sub(epoch)?)?;
        let tick = microseconds.checked_mul(multiplier).ok_or_else(overflow)?.div_euclid(divisor);
        ticks.append(tick)?;
    }
    Ok(ticks)
}
