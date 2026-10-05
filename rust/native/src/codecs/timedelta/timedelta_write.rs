use crate::codecs::options::Options;
use crate::python::references::PythonReferences;
use pyo3::types::{PyDelta, PyDeltaAccess};
use pyo3::{PyTraverseError, PyVisit};

use super::{MICROSECONDS_PER_SECOND, SECONDS_PER_DAY};
use pyo3::prelude::*;

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

impl PythonReferences for TimeDeltaWrite {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.fallback)?;
        visit.call(&self.validate)?;
        Ok(())
    }
}
