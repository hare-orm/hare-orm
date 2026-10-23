//! The options a codec is built with - a dict the Python side fills once per field.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};

/// A codec's options, read by key.
pub struct Options<'py> {
    field_name: &'py Bound<'py, PyString>,
    values: Bound<'py, PyDict>,
}

impl<'py> Options<'py> {
    pub fn new(field_name: &'py Bound<'py, PyString>, values: Bound<'py, PyDict>) -> Self {
        Options { field_name, values }
    }

    /// The interpreter the options were built in.
    pub fn py(&self) -> Python<'py> {
        self.values.py()
    }

    /// An error naming the field the options are for.
    pub fn error(&self, message: &str) -> PyErr {
        PyValueError::new_err(format!("codec of {}: {message}", self.field_name))
    }

    /// The value of `key`, None when it is missing or None.
    pub fn get_optional_object(&self, key: &str) -> PyResult<Option<Py<PyAny>>> {
        Ok(self.values.get_item(key)?.filter(|value| !value.is_none()).map(Bound::unbind))
    }

    /// The value of `key`.
    pub fn get_object(&self, key: &str) -> PyResult<Py<PyAny>> {
        self.get_optional_object(key)?.ok_or_else(|| self.error(&format!("missing option {key:?}")))
    }

    /// The bool value of `key`, False when it is missing.
    pub fn get_bool(&self, key: &str) -> PyResult<bool> {
        match self.values.get_item(key)? {
            Some(value) => value.extract(),
            None => Ok(false),
        }
    }

    /// The integer value of `key`.
    pub fn get_integer(&self, key: &str) -> PyResult<i64> {
        self.values.get_item(key)?.ok_or_else(|| self.error(&format!("missing option {key:?}")))?.extract()
    }

    /// The integer value of `key`, None when it is missing or None.
    pub fn get_optional_integer(&self, key: &str) -> PyResult<Option<i64>> {
        self.get_optional_object(key)?.map(|value| value.extract(self.values.py())).transpose()
    }

    /// The dict value of `key`.
    pub fn get_dict(&self, key: &str) -> PyResult<Py<PyDict>> {
        Ok(self.get_object(key)?.into_bound(self.values.py()).cast_into::<PyDict>()?.unbind())
    }

    /// The options of a codec nested in this one - under `key`, for the same field.
    pub fn get_nested(&self, key: &str) -> PyResult<Options<'py>> {
        Ok(Options::new(self.field_name, self.get_dict(key)?.into_bound(self.values.py())))
    }

    /// The str value of `key`.
    pub fn get_string(&self, key: &str) -> PyResult<String> {
        self.get_object(key)?.extract(self.values.py())
    }
}
