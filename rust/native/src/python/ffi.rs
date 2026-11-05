//! Safe wrappers over the CPython C API calls the hot loops use where PyO3's own API costs a lookup
//! or an allocation per call. Every `unsafe` block of the crate lives here.

use pyo3::exceptions::PyRuntimeError;
use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::PyString;

/// The error CPython set, or a `RuntimeError` naming `call` when it set none.
fn take_error(py: Python<'_>, call: &str) -> PyErr {
    PyErr::take(py).unwrap_or_else(|| PyRuntimeError::new_err(format!("{call} failed")))
}

/// `object.__delattr__(instance, name)` - bypasses a class's own `__setattr__`.
pub fn generic_delete_attribute(instance: &Bound<'_, PyAny>, name: &Bound<'_, PyString>) -> PyResult<()> {
    // SAFETY: both pointers are live borrowed references for the duration of the call; a null
    // value asks CPython to delete the attribute.
    let result = unsafe { ffi::PyObject_GenericSetAttr(instance.as_ptr(), name.as_ptr(), std::ptr::null_mut()) };
    if result == 0 {
        Ok(())
    } else {
        Err(take_error(instance.py(), "PyObject_GenericSetAttr"))
    }
}

/// `object.__setattr__(instance, name, value)` - bypasses a class's own `__setattr__`.
pub fn generic_set_attribute(
    instance: &Bound<'_, PyAny>,
    name: &Bound<'_, PyString>,
    value: &Bound<'_, PyAny>,
) -> PyResult<()> {
    // SAFETY: all three pointers are live borrowed references for the duration of the call.
    let result = unsafe { ffi::PyObject_GenericSetAttr(instance.as_ptr(), name.as_ptr(), value.as_ptr()) };
    if result == 0 {
        Ok(())
    } else {
        Err(take_error(instance.py(), "PyObject_GenericSetAttr"))
    }
}

/// The value of an exact `int` that fits 64 signed bits, None for a larger one.
pub fn get_i64(value: &Bound<'_, PyAny>) -> Option<i64> {
    let mut overflow: std::ffi::c_int = 0;
    // SAFETY: `value` is a live int; PyLong_AsLongLongAndOverflow reports a value out of range in
    // `overflow` instead of raising, and fails otherwise only for a non-int, which the callers rule out.
    let number = unsafe { ffi::PyLong_AsLongLongAndOverflow(value.as_ptr(), &raw mut overflow) };
    (overflow == 0).then_some(number)
}
