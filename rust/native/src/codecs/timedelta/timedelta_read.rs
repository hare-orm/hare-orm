use crate::codecs::options::Options;
use crate::python::references::PythonReferences;
use pyo3::types::{PyDelta, PyInt};
use pyo3::{PyTraverseError, PyVisit};

use super::{MICROSECONDS_PER_SECOND, SECONDS_PER_DAY};
use pyo3::prelude::*;

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

impl PythonReferences for TimeDeltaRead {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        Ok(())
    }
}
