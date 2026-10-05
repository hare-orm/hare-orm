//! A `TimeDeltaField`, stored as a BIGINT of microseconds.

use pyo3::prelude::*;
use pyo3::types::{PyDelta, PyDeltaAccess, PyInt};
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::options::Options;
use crate::python::references::PythonReferences;

const MICROSECONDS_PER_SECOND: i64 = 1_000_000;
const SECONDS_PER_DAY: i64 = 86_400;

pub struct TimeDeltaRead {
    fallback: Py<PyAny>,
}

impl TimeDeltaRead {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(TimeDeltaRead { fallback: options.get_object("fallback")? })
    }

    pub fn read<'py>(&self, raw: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = raw.py();
        if raw.is_none() || raw.is_instance_of::<PyDelta>() {
            return Ok(raw);
        }
        if raw.is_exact_instance_of::<PyInt>() {
            if let Ok(microseconds) = raw.extract::<i64>() {
                let seconds = microseconds.div_euclid(MICROSECONDS_PER_SECOND);
                let days = seconds.div_euclid(SECONDS_PER_DAY);
                if let Ok(days) = i32::try_from(days) {
                    let delta = PyDelta::new(
                        py,
                        days,
                        seconds.rem_euclid(SECONDS_PER_DAY) as i32,
                        microseconds.rem_euclid(MICROSECONDS_PER_SECOND) as i32,
                        false,
                    );
                    if let Ok(delta) = delta {
                        return Ok(delta.into_any());
                    }
                }
            }
        }
        // Out of timedelta's range, a float or a Decimal - rounded and checked by the field.
        self.fallback.bind(py).call1((raw,))
    }
}

pub struct TimeDeltaWrite {
    fallback: Py<PyAny>,
    validate: Option<Py<PyAny>>,
    null: bool,
}

impl TimeDeltaWrite {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(TimeDeltaWrite {
            fallback: options.get_object("fallback")?,
            validate: options.get_optional_object("validate")?,
            null: options.get_bool("null")?,
        })
    }

    pub fn write<'py>(&self, value: Bound<'py, PyAny>, instance: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() && self.null {
            return Ok(value);
        }
        let Ok(delta) = value.cast_exact::<PyDelta>() else {
            return self.fallback.bind(py).call1((value, instance));
        };
        let microseconds = (i128::from(delta.get_days()) * i128::from(SECONDS_PER_DAY)
            + i128::from(delta.get_seconds()))
            * i128::from(MICROSECONDS_PER_SECOND)
            + i128::from(delta.get_microseconds());
        let Ok(microseconds) = i64::try_from(microseconds) else {
            return self.fallback.bind(py).call1((value, instance));
        };
        if let Some(validate) = &self.validate {
            validate.bind(py).call1((&value,))?;
        }
        Ok(microseconds.into_pyobject(py)?.into_any())
    }
}

impl PythonReferences for TimeDeltaRead {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        Ok(())
    }
}

impl PythonReferences for TimeDeltaWrite {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        visit.call(&self.validate)?;
        Ok(())
    }
}
