//! An `IntEnumField`/`CharEnumField`: read through the enum's value-to-member map, a member of the
//! field's own enum written as its stored value.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyType};
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::options::Options;
use crate::python::references::PythonReferences;

pub struct EnumerationRead {
    /// `enum_type._value2member_map_`.
    members_by_value: Py<PyDict>,
    fallback: Py<PyAny>,
}

impl EnumerationRead {
    pub fn from_options(options: &Options<'_>) -> PyResult<Self> {
        Ok(EnumerationRead {
            members_by_value: options.get_dict("members_by_value")?,
            fallback: options.get_object("fallback")?,
        })
    }

    pub fn read<'py>(&self, raw: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = raw.py();
        if raw.is_none() {
            return Ok(raw);
        }
        // A stored text of a non-str value, or an unhashable value - the field's own lookup.
        match self.members_by_value.bind(py).get_item(&raw) {
            Ok(Some(member)) => Ok(member),
            _ => self.fallback.bind(py).call1((raw,)),
        }
    }
}

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

impl PythonReferences for EnumerationRead {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.members_by_value)?;
        visit.call(&self.fallback)?;
        Ok(())
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
