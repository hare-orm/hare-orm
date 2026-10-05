//! A column's two codecs - reading its driver values and writing its attribute values.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyType};
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::options::Options;
use crate::codecs::read_codec::ReadCodec;
use crate::codecs::write_codec::WriteCodec;
use crate::pg::result_cell::ResultCell;
use crate::pg::value::Value;
use crate::python::references::PythonReferences;

/// One column of a model: its attribute name and its two codecs.
#[pyclass(frozen, module = "rust.native.rows")]
pub struct FieldCodec {
    pub name: Py<PyString>,
    pub read: ReadCodec,
    pub write: Option<WriteCodec>,
}

impl FieldCodec {
    /// The attribute value of `raw`, a value the driver returned.
    pub fn read_value<'py>(&self, raw: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.read.read(raw)
    }

    /// What `write_value()` binds, as a `rust.native.pg` value: the outer error is the codec's own
    /// (raised at once, as by `write_value()`), the inner one reading the bound value - raised when
    /// a statement binds it, as a list of bound values raises it.
    pub fn write_parameter<'py>(
        &self,
        value: Bound<'py, PyAny>,
        instance: &Bound<'py, PyAny>,
    ) -> PyResult<Result<Value, PyErr>> {
        if let Some(WriteCodec::Json(codec)) = &self.write {
            if let Some(text) = codec.write_text(&value)? {
                return Ok(Ok(Value::Text(text)));
            }
        }
        Ok(self.write_value(value, instance)?.extract::<Value>())
    }

    /// The attribute value of `cell`, a value of a `rust.native.pg` result.
    pub fn read_cell<'py>(&self, py: Python<'py>, cell: &ResultCell<'_>) -> PyResult<Bound<'py, PyAny>> {
        self.read.read_cell(py, cell)
    }

    /// The value bound for `value`, an attribute value of `instance` (a model instance, the model,
    /// or None).
    pub fn write_value<'py>(
        &self,
        value: Bound<'py, PyAny>,
        instance: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        let Some(write) = &self.write else {
            return Err(pyo3::exceptions::PyTypeError::new_err(format!(
                "the column of {} is read-only",
                self.name.bind(py)
            )));
        };
        write.write(value, instance, self.name.bind(py))
    }
}

#[pymethods]
impl FieldCodec {
    /// A codec of the attribute `name`: the read codec `read_type` built with `read_options`, and
    /// the write codec `write_type` with `write_options` - None for a column never written.
    #[new]
    #[pyo3(signature = (name, read_type, read_options, write_type=None, write_options=None))]
    fn new(
        name: Bound<'_, PyString>,
        read_type: &str,
        read_options: Bound<'_, PyDict>,
        write_type: Option<&str>,
        write_options: Option<Bound<'_, PyDict>>,
    ) -> PyResult<Self> {
        let read = ReadCodec::build(read_type, &Options::new(&name, read_options))?;
        let write = match write_type {
            Some(codec_type) => {
                let write_options = write_options.unwrap_or_else(|| PyDict::new(name.py()));
                Some(WriteCodec::build(codec_type, &Options::new(&name, write_options))?)
            }
            None => None,
        };
        Ok(FieldCodec { name: name.unbind(), read, write })
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.name)?;
        self.read.traverse(&visit)?;
        if let Some(write) = &self.write {
            write.traverse(&visit)?;
        }
        Ok(())
    }

    #[getter]
    fn name(&self, py: Python<'_>) -> Py<PyString> {
        self.name.clone_ref(py)
    }

    /// The attribute value of a driver value.
    fn read<'py>(&self, raw: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.read_value(raw)
    }

    /// The bound value of an attribute value.
    #[pyo3(signature = (value, instance=None))]
    fn write<'py>(&self, value: Bound<'py, PyAny>, instance: Option<Bound<'py, PyAny>>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        let instance = instance.unwrap_or_else(|| py.None().into_bound(py));
        self.write_value(value, &instance)
    }

    /// The bound values of a list, tuple or set of attribute values, in its order - a None kept as
    /// it is, or left out with `skip_none`; None when a value other than None isn't of `value_type`.
    #[pyo3(signature = (values, instance=None, value_type=None, skip_none=false))]
    fn write_list<'py>(
        &self,
        values: &Bound<'py, PyAny>,
        instance: Option<&Bound<'py, PyAny>>,
        value_type: Option<&Bound<'py, PyType>>,
        skip_none: bool,
    ) -> PyResult<Option<Bound<'py, PyList>>> {
        let py = values.py();
        let instance = instance.map_or_else(|| py.None().into_bound(py), Bound::clone);
        let mut written = Vec::with_capacity(values.len().unwrap_or(0));
        for value in values.try_iter()? {
            let value = value?;
            if value.is_none() {
                if !skip_none {
                    written.push(value);
                }
                continue;
            }
            if value_type.is_some_and(|value_type| !value.get_type().is(value_type)) {
                return Ok(None);
            }
            written.push(self.write_value(value, &instance)?);
        }
        Ok(Some(PyList::new(py, written)?))
    }
}
