//! Safe wrappers over the CPython C API calls the hot loops use where PyO3's own API costs a lookup
//! or an allocation per call. Every `unsafe` block of the crate lives here.

use pyo3::exceptions::{PyRuntimeError, PyTypeError};
use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple, PyType};

use crate::pg::row::PgRow;

extern "C" {
    // Not exposed by pyo3::ffi: a dict allocated for its final size up front.
    fn _PyDict_NewPresized(minused: ffi::Py_ssize_t) -> *mut ffi::PyObject;
}

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

/// The built-in type a value is exactly an instance of, of those JSON text is made of.
pub enum JsonType {
    None,
    Bool,
    Int,
    Float,
    Str,
    Dict,
    List,
    Tuple,
    /// Any other type, subclasses of these included.
    Other,
}

/// The `JsonType` of `value` - one comparison of its type per candidate.
pub fn get_json_type(value: &Bound<'_, PyAny>) -> JsonType {
    let value_type = value.get_type_ptr();
    // Only the addresses of the static type objects are taken.
    let types = [
        (&raw mut ffi::PyLong_Type, JsonType::Int),
        (&raw mut ffi::PyUnicode_Type, JsonType::Str),
        (&raw mut ffi::PyFloat_Type, JsonType::Float),
        (&raw mut ffi::PyDict_Type, JsonType::Dict),
        (&raw mut ffi::PyList_Type, JsonType::List),
        (&raw mut ffi::PyBool_Type, JsonType::Bool),
        (&raw mut ffi::PyTuple_Type, JsonType::Tuple),
    ];
    if value.is_none() {
        return JsonType::None;
    }
    types
        .into_iter()
        .find(|(json_type_object, _)| std::ptr::eq(*json_type_object, value_type))
        .map_or(JsonType::Other, |(_, json_type)| json_type)
}

/// The value of an exact `int` that fits 64 signed bits, None for a larger one.
pub fn get_i64(value: &Bound<'_, PyAny>) -> Option<i64> {
    let mut overflow: std::ffi::c_int = 0;
    // SAFETY: `value` is a live int; PyLong_AsLongLongAndOverflow reports a value out of range in
    // `overflow` instead of raising, and fails otherwise only for a non-int, which the callers rule out.
    let number = unsafe { ffi::PyLong_AsLongLongAndOverflow(value.as_ptr(), &raw mut overflow) };
    (overflow == 0).then_some(number)
}

/// Calls `visit` with each item of `list` as a borrowed reference, while it returns true.
///
/// Returns:
///     False when `visit` stopped the walk.
pub fn all_list_items<'py>(list: &Bound<'py, PyList>, mut visit: impl FnMut(&Bound<'py, PyAny>) -> bool) -> bool {
    let py = list.py();
    let mut index = 0;
    loop {
        // SAFETY: the GIL is held and `visit` runs no Python code, so the list keeps its size and
        // items for the whole walk; an index below the size reads a live item, borrowed from it.
        let item = unsafe {
            if index >= ffi::PyList_GET_SIZE(list.as_ptr()) {
                return true;
            }
            Borrowed::from_ptr(py, ffi::PyList_GET_ITEM(list.as_ptr(), index))
        };
        if !visit(&item) {
            return false;
        }
        index += 1;
    }
}

/// Calls `visit` with each item of `tuple` as a borrowed reference, while it returns true.
///
/// Returns:
///     False when `visit` stopped the walk.
pub fn all_tuple_items<'py>(tuple: &Bound<'py, PyTuple>, mut visit: impl FnMut(&Bound<'py, PyAny>) -> bool) -> bool {
    let py = tuple.py();
    // SAFETY: a tuple never changes; every index below its size reads a live item, borrowed from it.
    let size = unsafe { ffi::PyTuple_GET_SIZE(tuple.as_ptr()) };
    for index in 0..size {
        // SAFETY: as above.
        let item = unsafe { Borrowed::from_ptr(py, ffi::PyTuple_GET_ITEM(tuple.as_ptr(), index)) };
        if !visit(&item) {
            return false;
        }
    }
    true
}

/// Calls `visit` with each key and value of `dict` as borrowed references, while it returns true.
///
/// Returns:
///     False when `visit` stopped the walk.
pub fn all_dict_items<'py>(
    dict: &Bound<'py, PyDict>,
    mut visit: impl FnMut(&Bound<'py, PyAny>, &Bound<'py, PyAny>) -> bool,
) -> bool {
    let py = dict.py();
    let mut position: ffi::Py_ssize_t = 0;
    let mut key = std::ptr::null_mut();
    let mut value = std::ptr::null_mut();
    // SAFETY: the GIL is held and `visit` runs no Python code, so the dict is unchanged for the whole
    // walk; PyDict_Next hands out borrowed references to its live keys and values.
    while unsafe { ffi::PyDict_Next(dict.as_ptr(), &raw mut position, &raw mut key, &raw mut value) } != 0 {
        // SAFETY: as above - both pointers are live, borrowed from the dict.
        let (key, value) = unsafe { (Borrowed::from_ptr(py, key), Borrowed::from_ptr(py, value)) };
        if !visit(&key, &value) {
            return false;
        }
    }
    true
}

/// A dict sized for `size` keys, set as `instance.__dict__`.
pub fn set_presized_instance_dict<'py>(instance: &Bound<'py, PyAny>, size: usize) -> PyResult<Bound<'py, PyDict>> {
    let py = instance.py();
    // SAFETY: _PyDict_NewPresized returns a new reference or NULL with an error set.
    let pointer = unsafe { _PyDict_NewPresized(size as ffi::Py_ssize_t) };
    if pointer.is_null() {
        return Err(take_error(py, "_PyDict_NewPresized"));
    }
    // SAFETY: a non-NULL result is a new reference to a dict.
    let dict = unsafe { Bound::from_owned_ptr(py, pointer) }.cast_into::<PyDict>()?;
    // SAFETY: both pointers are live; PyObject_GenericSetDict does not steal the dict reference.
    let result = unsafe { ffi::PyObject_GenericSetDict(instance.as_ptr(), dict.as_ptr(), std::ptr::null_mut()) };
    if result != 0 {
        return Err(take_error(py, "PyObject_GenericSetDict"));
    }
    Ok(dict)
}

/// `row[index]` - read straight off a `rust.native.pg` row, else through the sequence protocol,
/// then the mapping protocol for a row without one.
pub fn get_row_item<'py>(row: &Bound<'py, PyAny>, index: usize) -> PyResult<Bound<'py, PyAny>> {
    let py = row.py();
    if let Ok(pg_row) = row.cast_exact::<PgRow>() {
        if let Some(value) = pg_row.get().get_value(py, index) {
            return Ok(value);
        }
    }
    // SAFETY: `row` is a live reference; a NULL result is checked before use.
    let pointer = unsafe { ffi::PySequence_GetItem(row.as_ptr(), index as ffi::Py_ssize_t) };
    if pointer.is_null() {
        let _ = PyErr::take(py);
        return row.get_item(index);
    }
    // SAFETY: PySequence_GetItem returns a new reference.
    Ok(unsafe { Bound::from_owned_ptr(py, pointer) })
}

/// A new instance of `model_class` with no `__init__` run - what `cls.__new__(cls)` gives a class
/// that defines no `__new__` of its own.
pub fn allocate_instance<'py>(model_class: &Bound<'py, PyType>) -> PyResult<Bound<'py, PyAny>> {
    let py = model_class.py();
    let type_pointer = model_class.as_ptr().cast::<ffi::PyTypeObject>();
    // A static type's own state is not set up by a zeroed allocation.
    // SAFETY: `type_pointer` points at a live type object.
    if unsafe { ffi::PyType_HasFeature(type_pointer, ffi::Py_TPFLAGS_HEAPTYPE) } == 0 {
        return Err(PyTypeError::new_err(format!("{} is not a class defined in Python", model_class.name()?)));
    }
    // SAFETY: a heap type allocates a zeroed instance its deallocator handles.
    let pointer = unsafe { ffi::PyType_GenericAlloc(type_pointer, 0) };
    if pointer.is_null() {
        return Err(take_error(py, "PyType_GenericAlloc"));
    }
    // SAFETY: PyType_GenericAlloc returns a new reference.
    Ok(unsafe { Bound::from_owned_ptr(py, pointer) })
}

/// A list of `length` items filled by position, each exactly once.
pub struct ListBuilder<'py> {
    list: Bound<'py, PyList>,
    length: usize,
}

impl<'py> ListBuilder<'py> {
    pub fn new(py: Python<'py>, length: usize) -> PyResult<Self> {
        // SAFETY: PyList_New returns a new reference or NULL with an error set.
        let pointer = unsafe { ffi::PyList_New(length as ffi::Py_ssize_t) };
        if pointer.is_null() {
            return Err(take_error(py, "PyList_New"));
        }
        // SAFETY: a non-NULL result is a new reference to a list.
        let list = unsafe { Bound::from_owned_ptr(py, pointer) }.cast_into::<PyList>()?;
        Ok(Self { list, length })
    }

    /// Puts `item` at `index`. A slot set twice keeps the first item's reference forever; a slot never
    /// set stays NULL, which the list's deallocator skips.
    pub fn set(&mut self, index: usize, item: Bound<'py, PyAny>) {
        assert!(index < self.length, "list index {index} out of range {}", self.length);
        // SAFETY: the index is in range; PyList_SET_ITEM steals the item's reference.
        unsafe { ffi::PyList_SET_ITEM(self.list.as_ptr(), index as ffi::Py_ssize_t, item.into_ptr()) };
    }

    pub fn finish(self) -> Bound<'py, PyList> {
        self.list
    }
}
