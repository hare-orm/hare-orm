//! Model construction preserving Python conversion and assignment order.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyString, PyTuple};
use pyo3::{PyTraverseError, PyVisit};

use crate::python::ffi::generic_set_attribute;
use crate::rows::column_reader::ColumnReader;

pub(crate) struct ColumnEntry {
    name: Py<PyString>,
    key: Py<PyAny>,
    reader: ColumnReader,
}

impl ColumnEntry {
    pub(crate) fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.name)?;
        visit.call(&self.key)?;
        self.reader.traverse(visit)
    }
}

/// How the instances of one model are made from a result's rows - its columns, their readers and the
/// model's flags - made once from its `ModelColumns` and kept with them (a plan keeps them), never per
/// query. The model's public `__new__` - looked up for each row, so a replaced one counts from the next -
/// the field readers and descriptors run in row order.
#[pyclass(frozen, module = "rust.native.rows")]
pub struct ModelRowLayout {
    model: Py<PyAny>,
    entries: Vec<ColumnEntry>,
    partial: bool,
    related: bool,
    custom_pk: bool,
    track_dirty: bool,
    /// The relation path to the instance - empty for the queried model itself.
    pub path: Py<PyTuple>,
    /// The attribute of the parent instance a joined relation's instance is set to (`_<relation>`) -
    /// None for the queried model.
    pub parent_attribute: Option<Py<PyString>>,
}

#[pymethods]
impl ModelRowLayout {
    /// Args:
    ///     columns: The `ModelColumns` of the model.
    #[new]
    fn new(columns: &Bound<'_, PyAny>) -> PyResult<Self> {
        let py = columns.py();
        let model = columns.getattr(intern!(py, "model"))?;
        let meta = model.getattr(intern!(py, "_meta"))?;
        let names = columns.getattr(intern!(py, "field_names"))?.cast_into::<PyTuple>()?;
        let layout =
            columns.getattr(intern!(py, "row_layout"))?.getattr(intern!(py, "columns"))?.cast_into::<PyTuple>()?;
        if names.len() != layout.len() {
            return Err(pyo3::exceptions::PyValueError::new_err("field names and columns differ in length"));
        }
        let mut entries = Vec::with_capacity(names.len());
        for (name, column) in names.iter().zip(layout.iter()) {
            entries.push(ColumnEntry {
                name: name.cast_into::<PyString>()?.unbind(),
                key: column.getattr(intern!(py, "key"))?.unbind(),
                reader: ColumnReader::new(column.getattr(intern!(py, "reader"))?),
            });
        }
        let path = columns.getattr(intern!(py, "path"))?.cast_into::<PyTuple>()?;
        let parent_attribute = if path.is_empty() {
            None
        } else {
            let relation = path.get_item(path.len() - 1)?.extract::<String>()?;
            Some(PyString::new(py, &format!("_{relation}")).unbind())
        };
        Ok(ModelRowLayout {
            model: model.unbind(),
            entries,
            partial: columns.getattr(intern!(py, "is_partial"))?.is_truthy()?,
            related: columns.getattr(intern!(py, "is_related"))?.is_truthy()?,
            custom_pk: meta.getattr(intern!(py, "default_custom_generated_pk"))?.is_truthy()?,
            track_dirty: meta.getattr(intern!(py, "track_dirty_fields"))?.is_truthy()?,
            path: path.unbind(),
            parent_attribute,
        })
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.model)?;
        for entry in &self.entries {
            visit.call(&entry.name)?;
            visit.call(&entry.key)?;
            entry.reader.traverse(&visit)?;
        }
        visit.call(&self.path)?;
        visit.call(&self.parent_attribute)
    }
}

impl ModelRowLayout {
    /// The instance of a row - None for a joined relation whose columns are all NULL.
    pub fn read<'py>(
        &self,
        row: &Bound<'py, PyAny>,
        connection_alias: &Bound<'py, PyAny>,
        snapshot: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = row.py();
        if self.related {
            let mut absent = true;
            for entry in &self.entries {
                if !row.get_item(entry.key.bind(py))?.is_none() {
                    absent = false;
                    break;
                }
            }
            if absent {
                return Ok(py.None().into_bound(py));
            }
        }
        self.construct(self.decode(row), connection_alias, snapshot)
    }

    /// Decoding is lazy: ordinary hydration still interleaves readers and descriptors.
    /// Grouped reads collect these same values before selecting an instance by its decoded key.
    pub fn decode<'a, 'py>(
        &'a self,
        row: &'a Bound<'py, PyAny>,
    ) -> impl Iterator<Item = PyResult<Bound<'py, PyAny>>> + 'a {
        self.entries.iter().map(move |entry| entry.reader.read(row.get_item(entry.key.bind(row.py()))?))
    }

    pub fn field_position(&self, py: Python<'_>, name: &str) -> PyResult<usize> {
        for (position, entry) in self.entries.iter().enumerate() {
            if entry.name.bind(py).to_str()? == name {
                return Ok(position);
            }
        }
        Err(pyo3::exceptions::PyValueError::new_err(format!("key field {name} is not selected")))
    }

    /// Construction consumes decoded values, eagerly supplied or read on demand by `decode()`.
    pub fn construct<'py>(
        &self,
        values: impl Iterator<Item = PyResult<Bound<'py, PyAny>>>,
        connection_alias: &Bound<'py, PyAny>,
        snapshot: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = connection_alias.py();
        let truth = PyBool::new(py, true).to_owned().into_any();
        let model = self.model.bind(py);
        let instance = model.getattr(intern!(py, "__new__"))?.call1((model,))?;
        if self.partial {
            generic_set_attribute(&instance, intern!(py, "_partial"), &truth)?;
        }
        generic_set_attribute(&instance, intern!(py, "_saved_in_db"), &truth)?;
        if self.custom_pk {
            generic_set_attribute(&instance, intern!(py, "_custom_generated_pk"), &truth)?;
        }
        generic_set_attribute(&instance, intern!(py, "_connection_alias"), connection_alias)?;
        for (entry, value) in self.entries.iter().zip(values) {
            let value = value?;
            generic_set_attribute(&instance, entry.name.bind(py), &value)?;
        }
        if self.track_dirty {
            snapshot.call1((&instance,))?;
        }
        Ok(instance)
    }

    /// Classify annotation readers once for a row or a bounded batch; callbacks run for every value.
    pub(crate) fn prepare_annotations<'py>(
        annotations: Vec<(Bound<'py, PyString>, Bound<'py, PyAny>, Bound<'py, PyAny>)>,
    ) -> Vec<ColumnEntry> {
        annotations
            .into_iter()
            .map(|(name, key, reader)| ColumnEntry {
                name: name.unbind(),
                key: key.unbind(),
                reader: ColumnReader::new(reader),
            })
            .collect()
    }

    pub(crate) fn assign_annotations(
        instance: &Bound<'_, PyAny>,
        row: &Bound<'_, PyAny>,
        annotations: &[ColumnEntry],
    ) -> PyResult<()> {
        let py = row.py();
        for entry in annotations {
            let value = entry.reader.read(row.get_item(entry.key.bind(py))?)?;
            generic_set_attribute(instance, entry.name.bind(py), &value)?;
        }
        Ok(())
    }
}

/// The native layout kept with a `ModelColumns` (`ModelColumns.native_layout`).
pub fn get_layout<'py>(columns: &Bound<'py, PyAny>) -> PyResult<Bound<'py, ModelRowLayout>> {
    Ok(columns.getattr(intern!(columns.py(), "native_layout"))?.cast_into::<ModelRowLayout>()?)
}
