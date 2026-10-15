//! Writing an int, str, float or bool column: a value of exactly the field's type is checked here
//! (a null byte in a str, NaN, then the field's validators) and bound as it is.

use pyo3::prelude::*;
use pyo3::types::{PyFloat, PyString, PyType};
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::inline_checks::InlineChecks;
use crate::codecs::options::Options;
use crate::python::references::PythonReferences;

/// `NativeWriteCheck.NO_NULL_BYTE` - the str must hold no null byte.
const CHECK_NO_NULL_BYTE: i64 = 1;
/// `NativeWriteCheck.NOT_NAN` - the float must not be NaN.
const CHECK_NOT_NAN: i64 = 2;

pub struct ScalarWrite {
    exact_type: Py<PyType>,
    check: i64,
    checks: InlineChecks,
    /// `field.validate` when a validator isn't inlined.
    validate: Option<Py<PyAny>>,
    null: bool,
    fallback: Py<PyAny>,
}

impl ScalarWrite {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        let py = options.py();
        Ok(ScalarWrite {
            exact_type: options.get_object("exact_type")?.into_bound(py).cast_into::<PyType>()?.unbind(),
            check: options.get_optional_integer("check")?.unwrap_or(0),
            checks: InlineChecks::from_options(options)?,
            validate: options.get_optional_object("validate")?,
            null: options.get_bool("null")?,
            fallback: options.get_object("fallback")?,
        })
    }

    fn fails_check(&self, value: &Bound<'_, PyAny>) -> bool {
        match self.check {
            // A str that isn't valid UTF-8 (lone surrogates) goes to the field's own to_db_value.
            CHECK_NO_NULL_BYTE => {
                value.cast::<PyString>().ok().and_then(|text| text.to_str().ok()).is_none_or(|text| text.contains('\0'))
            }
            CHECK_NOT_NAN => value.cast::<PyFloat>().is_ok_and(|number| number.value().is_nan()),
            _ => false,
        }
    }

    pub fn write<'py>(
        &self,
        value: Bound<'py, PyAny>,
        instance: &Bound<'py, PyAny>,
        field_name: &Bound<'py, PyString>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() && self.null {
            return Ok(value);
        }
        if !value.get_type().is(self.exact_type.bind(py)) || self.fails_check(&value) {
            return self.fallback.bind(py).call1((value, instance));
        }
        match &self.validate {
            Some(validate) => {
                validate.bind(py).call1((&value,))?;
            }
            None => self.checks.check(&value, field_name)?,
        }
        Ok(value)
    }
}

impl PythonReferences for ScalarWrite {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.exact_type)?;
        visit.call(&self.validate)?;
        visit.call(&self.fallback)?;
        self.checks.traverse(visit)?;
        Ok(())
    }
}
