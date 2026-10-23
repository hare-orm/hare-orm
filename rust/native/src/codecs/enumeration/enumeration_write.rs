use crate::codecs::options::Options;
use crate::python::references::PythonReferences;
use pyo3::types::{PyDict, PyType};
use pyo3::{PyTraverseError, PyVisit};

use pyo3::prelude::*;

pub struct EnumerationWrite {
    enum_type: Py<PyType>,
    /// Each member of `enum_type` and the value stored for it.
    stored_values_by_member: Py<PyDict>,
    validate: Option<Py<PyAny>>,
    null: bool,
    fallback: Py<PyAny>,
}

impl EnumerationWrite {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(EnumerationWrite {
            enum_type: options.get_object("enum_type")?.into_bound(options.py()).cast_into::<PyType>()?.unbind(),
            stored_values_by_member: options.get_dict("stored_values_by_member")?,
            validate: options.get_optional_object("validate")?,
            null: options.get_bool("null")?,
            fallback: options.get_object("fallback")?,
        })
    }

    pub fn write<'py>(&self, value: Bound<'py, PyAny>, instance: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        if value.is_none() && self.null {
            return Ok(value);
        }
        if !value.get_type().is(self.enum_type.bind(py)) {
            return self.fallback.bind(py).call1((value, instance));
        }
        let Some(stored_value) = self.stored_values_by_member.bind(py).get_item(&value)? else {
            return self.fallback.bind(py).call1((value, instance));
        };
        if let Some(validate) = &self.validate {
            validate.bind(py).call1((&stored_value,))?;
        }
        Ok(stored_value)
    }
}

impl PythonReferences for EnumerationWrite {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.enum_type)?;
        visit.call(&self.stored_values_by_member)?;
        visit.call(&self.validate)?;
        visit.call(&self.fallback)?;
        Ok(())
    }
}
