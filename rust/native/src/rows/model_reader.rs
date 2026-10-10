//! Builds model instances from driver rows - what `ModelColumns.compile_hydrate_function()` does in
//! Python, for a whole result at once.

use pyo3::exceptions::{PyAttributeError, PyTypeError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyList, PyString, PyTuple, PyType};
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::field_codec::FieldCodec;
use crate::python::{deep_copy, ffi};
use crate::rows::result_row::ResultRow;
use crate::rows::result_rows::ResultRows;

/// From this many attributes an instance's dict is built up front, sized for all of them; below
/// it each attribute is set on the instance (CPython keeps a few attributes without a dict).
const PRESIZED_DICT_MIN_ATTRIBUTES: usize = 15;

/// Whether attribute lookup reads a dict `PyObject_GenericSetDict` gave an instance - found once per
/// interpreter.
static READS_REPLACED_INSTANCE_DICT: PyOnceLock<bool> = PyOnceLock::new();

/// Where an instance's attributes are written.
enum Attributes<'py> {
    Instance(Bound<'py, PyAny>),
    Dict(Bound<'py, PyDict>),
}

impl<'py> Attributes<'py> {
    fn set(&self, name: &Bound<'py, PyString>, value: &Bound<'py, PyAny>) -> PyResult<()> {
        match self {
            Attributes::Instance(instance) => ffi::generic_set_attribute(instance, name, value),
            Attributes::Dict(dict) => dict.set_item(name, value),
        }
    }
}

/// The baseline of `get_dirty_fields()` a model with `Meta.track_dirty_fields` takes on reading -
/// `DirtyFields.snapshot_dirty_fields()`.
struct DirtySnapshot {
    /// Each direct field, with the position of its column among the read ones - None for a field
    /// not read, taken from the instance when it has it.
    fields: Vec<(Py<PyString>, Option<usize>)>,
    /// The value types kept as they are; any other value is deep-copied.
    kept_types: Py<PyTuple>,
    /// `copy.deepcopy`.
    deepcopy: Py<PyAny>,
}

impl DirtySnapshot {
    fn take<'py>(&self, instance: &Bound<'py, PyAny>, values: &[Bound<'py, PyAny>]) -> PyResult<Bound<'py, PyDict>> {
        let py = instance.py();
        let snapshot = PyDict::new(py);
        let kept_types = self.kept_types.bind(py);
        for (name, position) in &self.fields {
            let name = name.bind(py);
            let value = match position {
                Some(position) => values[*position].clone(),
                None => match instance.getattr(name) {
                    Ok(value) => value,
                    Err(error) if error.is_instance_of::<PyAttributeError>(py) => continue,
                    Err(error) => return Err(error),
                },
            };
            let copy = if value.is_instance(kept_types)? {
                value
            } else if let Some(copy) = deep_copy::deep_copy_plain(&value)? {
                copy
            } else {
                self.deepcopy.bind(py).call1((value,))?
            };
            snapshot.set_item(name, copy)?;
        }
        Ok(snapshot)
    }
}

/// The columns of one model in a result, and how each is read.
#[pyclass(frozen, module = "rust.native.rows")]
pub struct ModelReader {
    model: Py<PyType>,
    codecs: Vec<Py<FieldCodec>>,
    is_partial: bool,
    custom_generated_pk: bool,
    dirty_snapshot: Option<DirtySnapshot>,
}

impl ModelReader {
    /// Whether an instance with `attribute_count` attributes gets a dict built up front. Not on
    /// Python 3.13: its attribute lookup keeps reading an instance's inline values after
    /// `PyObject_GenericSetDict` gives the instance a new dict, so every attribute is set on the
    /// instance there.
    fn uses_presized_dict(py: Python<'_>, attribute_count: usize) -> bool {
        attribute_count >= PRESIZED_DICT_MIN_ATTRIBUTES
            && *READS_REPLACED_INSTANCE_DICT.get_or_init(py, || {
                let version = py.version_info();
                !(version.major == 3 && version.minor == 13)
            })
    }

    /// Whether the row holds NULL in every column of the model - a LEFT JOIN that matched no row.
    fn is_all_null(row: &ResultRow<'_, '_>, positions: &[usize]) -> PyResult<bool> {
        for position in positions {
            if !row.is_null(*position)? {
                return Ok(false);
            }
        }
        Ok(true)
    }

    /// The instance of one row, its columns read from row positions `positions`, one per codec.
    pub fn read_instance<'py>(
        &self,
        py: Python<'py>,
        row: &ResultRow<'_, 'py>,
        positions: &[usize],
        connection_alias: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let instance = ffi::allocate_instance(self.model.bind(py))?;
        let attribute_count = self.codecs.len() + 4;
        let attributes = if Self::uses_presized_dict(py, attribute_count) {
            Attributes::Dict(ffi::set_presized_instance_dict(&instance, attribute_count)?)
        } else {
            Attributes::Instance(instance.clone())
        };
        // `_partial`, `_custom_generated_pk` and `_await_when_save` are the model's class defaults -
        // False, False and no pending default - set only where they differ.
        let true_value = pyo3::types::PyBool::new(py, true);
        if self.is_partial {
            attributes.set(intern!(py, "_partial"), true_value.as_any())?;
        }
        attributes.set(intern!(py, "_saved_in_db"), true_value.as_any())?;
        if self.custom_generated_pk {
            attributes.set(intern!(py, "_custom_generated_pk"), true_value.as_any())?;
        }
        if let Some(connection_alias) = connection_alias {
            attributes.set(intern!(py, "_connection_alias"), connection_alias)?;
        }
        let mut values = Vec::with_capacity(if self.dirty_snapshot.is_some() { self.codecs.len() } else { 0 });
        for (codec, position) in self.codecs.iter().zip(positions) {
            let codec = codec.get();
            let value = row.read(*position, codec)?;
            attributes.set(codec.name.bind(py), &value)?;
            if self.dirty_snapshot.is_some() {
                values.push(value);
            }
        }
        if let Some(dirty_snapshot) = &self.dirty_snapshot {
            attributes.set(intern!(py, "_dirty_snapshot"), dirty_snapshot.take(&instance, &values)?.as_any())?;
        }
        Ok(instance)
    }
}

#[pymethods]
impl ModelReader {
    /// A reader of `model`'s `codecs`, one per column in column order; `is_partial` - only some of the
    /// model's columns are selected; `custom_generated_pk` - the model's `default_custom_generated_pk`.
    /// With `dirty_fields` - the model's direct fields - each instance gets the baseline of
    /// `get_dirty_fields()`: values of `kept_types` as they are, any other value through `deepcopy`.
    #[new]
    #[pyo3(signature = (model, codecs, is_partial, custom_generated_pk, dirty_fields=None, kept_types=None, deepcopy=None))]
    fn new(
        model: Bound<'_, PyType>,
        codecs: Vec<Py<FieldCodec>>,
        is_partial: bool,
        custom_generated_pk: bool,
        dirty_fields: Option<Vec<Bound<'_, PyString>>>,
        kept_types: Option<Py<PyTuple>>,
        deepcopy: Option<Py<PyAny>>,
    ) -> PyResult<Self> {
        let dirty_snapshot = match (dirty_fields, kept_types, deepcopy) {
            (Some(dirty_fields), Some(kept_types), Some(deepcopy)) => {
                let mut fields = Vec::with_capacity(dirty_fields.len());
                for name in dirty_fields {
                    let mut position = None;
                    for (index, codec) in codecs.iter().enumerate() {
                        if codec.get().name.bind(name.py()).as_any().eq(&name)? {
                            position = Some(index);
                        }
                    }
                    fields.push((name.unbind(), position));
                }
                Some(DirtySnapshot { fields, kept_types, deepcopy })
            }
            (None, _, _) => None,
            _ => return Err(PyTypeError::new_err("dirty_fields needs kept_types and deepcopy")),
        };
        Ok(ModelReader { model: model.unbind(), codecs, is_partial, custom_generated_pk, dirty_snapshot })
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.model)?;
        for codec in &self.codecs {
            visit.call(codec)?;
        }
        if let Some(dirty_snapshot) = &self.dirty_snapshot {
            for (name, _position) in &dirty_snapshot.fields {
                visit.call(name)?;
            }
            visit.call(&dirty_snapshot.kept_types)?;
            visit.call(&dirty_snapshot.deepcopy)?;
        }
        Ok(())
    }

    /// The instances of `rows` - a `PgResult` or a sequence of driver rows - read by position from
    /// `column_offset`, the row position of the model's first column. `connection_alias` is set as
    /// each instance's `_connection_alias` (None leaves it unset); with `none_when_all_null` a row
    /// with NULL in every column of the model gives None.
    #[pyo3(signature = (rows, connection_alias=None, column_offset=0, none_when_all_null=false))]
    fn read<'py>(
        &self,
        rows: &Bound<'py, PyAny>,
        connection_alias: Option<&Bound<'py, PyAny>>,
        column_offset: usize,
        none_when_all_null: bool,
    ) -> PyResult<Bound<'py, PyList>> {
        let py = rows.py();
        let rows = ResultRows::new(rows)?;
        let positions: Vec<usize> = (column_offset..column_offset + self.codecs.len()).collect();
        let mut instances = ffi::ListBuilder::new(py, rows.len())?;
        for index in 0..rows.len() {
            let row = rows.row(index)?;
            let item = if none_when_all_null && Self::is_all_null(&row, &positions)? {
                py.None().into_bound(py)
            } else {
                self.read_instance(py, &row, &positions, connection_alias)?
            };
            instances.set(index, item);
        }
        Ok(instances.finish())
    }
}
