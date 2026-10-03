//! Builds a new model instance from its constructor's keyword arguments - the common shapes;
//! anything else is left to `Model.__init__`, before the instance is touched.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyString, PyType};
use pyo3::{PyTraverseError, PyVisit};

use crate::python::ffi;

/// How a value given for a column is assigned.
struct ColumnAssignment {
    name: Py<PyString>,
    /// The exact types the field's `to_python` returns unchanged.
    normalized_types: Vec<Py<PyType>>,
    /// The field - its `to_python` converts any other value.
    field: Py<PyAny>,
    null: bool,
    is_primary_key: bool,
    is_generated_primary_key: bool,
}

/// How a related instance given for a forward relation is assigned.
struct RelationAssignment {
    related_model: Py<PyType>,
    null: bool,
    /// The relation's key columns on this model, and the related fields they hold.
    source_fields: Vec<Py<PyString>>,
    to_fields: Vec<Py<PyString>>,
    /// The attribute holding the related instance.
    cache_name: Py<PyString>,
    /// The `defaults` entries of the key columns - set by the relation.
    source_default_indexes: Vec<usize>,
}

/// A field's value when the constructor isn't given one.
enum DefaultValue {
    /// The same immutable value on every instance.
    Constant(Py<PyAny>),
    /// Worked out by `Model._assign_default()` with this setter.
    Field(Py<PyAny>),
}

struct DefaultAssignment {
    name: Py<PyString>,
    value: DefaultValue,
}

/// Builds instances of one model.
#[pyclass(frozen, module = "rust.native.rows")]
pub struct ModelConstructor {
    /// Column name -> index in `columns`.
    column_indexes: Py<PyDict>,
    columns: Vec<ColumnAssignment>,
    /// Relation name -> index in `relations`.
    relation_indexes: Py<PyDict>,
    relations: Vec<RelationAssignment>,
    defaults: Vec<DefaultAssignment>,
    /// `hare.query.expressions.Expression` - assigned as it is.
    expression_type: Py<PyType>,
    /// `Model._assign_default`.
    assign_default: Py<PyAny>,
}

type ColumnSpec = (Py<PyString>, Vec<Py<PyType>>, Py<PyAny>, bool, bool, bool);
type RelationSpec = (Py<PyString>, Py<PyType>, bool, Vec<Py<PyString>>, Vec<Py<PyString>>, Py<PyString>);
type DefaultSpec = (Py<PyString>, bool, Py<PyAny>);

impl ModelConstructor {
    /// The value assigned for `value` given for `column`; None when the constructor leaves it to
    /// `Model.__init__`.
    fn get_column_value<'py>(
        &self,
        column: &ColumnAssignment,
        value: Bound<'py, PyAny>,
    ) -> PyResult<Option<Bound<'py, PyAny>>> {
        let py = value.py();
        let value_type = value.get_type();
        if column.normalized_types.iter().any(|normalized_type| value_type.is(normalized_type.bind(py))) {
            return Ok(Some(value));
        }
        if value.is_none() && (!column.null || column.is_primary_key) {
            return Ok(None);
        }
        if !value.is_none() && value.is_instance(self.expression_type.bind(py))? {
            return Ok(Some(value));
        }
        if value.is_callable() {
            return Ok(None);
        }
        column.field.bind(py).call_method1(intern!(py, "to_python"), (value,)).map(Some)
    }

    /// The key column values and the related instance assigned for `value` given for `relation`;
    /// false when the constructor leaves it to `Model.__init__`.
    fn add_relation_values<'py>(
        relation: &RelationAssignment,
        value: &Bound<'py, PyAny>,
        kwargs: &Bound<'py, PyDict>,
        assignments: &mut Vec<(Bound<'py, PyString>, Bound<'py, PyAny>)>,
    ) -> PyResult<bool> {
        let py = value.py();
        for source_field in &relation.source_fields {
            if kwargs.contains(source_field.bind(py))? {
                // Both given - checked against each other by Model.__init__.
                return Ok(false);
            }
        }
        if value.is_none() {
            if !relation.null {
                return Ok(false);
            }
            for source_field in &relation.source_fields {
                assignments.push((source_field.bind(py).clone(), value.clone()));
            }
        } else {
            if !value.get_type().is(relation.related_model.bind(py)) {
                return Ok(false);
            }
            let Ok(saved_in_db) = value.getattr(intern!(py, "_saved_in_db")) else {
                return Ok(false);
            };
            if !saved_in_db.is_truthy()? {
                return Ok(false);
            }
            for (source_field, to_field) in relation.source_fields.iter().zip(&relation.to_fields) {
                match value.getattr(to_field.bind(py)) {
                    Ok(to_field_value) if !to_field_value.is_none() => {
                        assignments.push((source_field.bind(py).clone(), to_field_value));
                    }
                    _ => return Ok(false),
                }
            }
        }
        assignments.push((relation.cache_name.bind(py).clone(), value.clone()));
        Ok(true)
    }
}

#[pymethods]
impl ModelConstructor {
    /// A constructor of `columns` (name, normalized types, field, null, is the primary
    /// key, is a generated primary key), forward `relations` (name, related model, null, key
    /// columns, related fields, cache attribute) and `defaults` (name, is a constant, the constant
    /// or the setter `assign_default` uses), in that order of assignment.
    #[new]
    fn new(
        py: Python<'_>,
        columns: Vec<ColumnSpec>,
        relations: Vec<RelationSpec>,
        defaults: Vec<DefaultSpec>,
        expression_type: Py<PyType>,
        assign_default: Py<PyAny>,
    ) -> PyResult<Self> {
        let column_indexes = PyDict::new(py);
        let mut column_assignments = Vec::with_capacity(columns.len());
        for (index, (name, normalized_types, field, null, is_primary_key, is_generated_primary_key)) in
            columns.into_iter().enumerate()
        {
            column_indexes.set_item(name.bind(py), index)?;
            column_assignments.push(ColumnAssignment {
                name,
                normalized_types,
                field,
                null,
                is_primary_key,
                is_generated_primary_key,
            });
        }
        let default_assignments: Vec<DefaultAssignment> = defaults
            .into_iter()
            .map(|(name, is_constant, value)| DefaultAssignment {
                name,
                value: if is_constant { DefaultValue::Constant(value) } else { DefaultValue::Field(value) },
            })
            .collect();
        let relation_indexes = PyDict::new(py);
        let mut relation_assignments = Vec::with_capacity(relations.len());
        for (index, (name, related_model, null, source_fields, to_fields, cache_name)) in
            relations.into_iter().enumerate()
        {
            relation_indexes.set_item(name.bind(py), index)?;
            let mut source_default_indexes = Vec::new();
            for source_field in &source_fields {
                for (default_index, default) in default_assignments.iter().enumerate() {
                    if default.name.bind(py).as_any().eq(source_field.bind(py))? {
                        source_default_indexes.push(default_index);
                    }
                }
            }
            relation_assignments.push(RelationAssignment {
                related_model,
                null,
                source_fields,
                to_fields,
                cache_name,
                source_default_indexes,
            });
        }
        Ok(ModelConstructor {
            column_indexes: column_indexes.unbind(),
            columns: column_assignments,
            relation_indexes: relation_indexes.unbind(),
            relations: relation_assignments,
            defaults: default_assignments,
            expression_type,
            assign_default,
        })
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.column_indexes)?;
        for column in &self.columns {
            visit.call(&column.name)?;
            for normalized_type in &column.normalized_types {
                visit.call(normalized_type)?;
            }
            visit.call(&column.field)?;
        }
        visit.call(&self.relation_indexes)?;
        for relation in &self.relations {
            visit.call(&relation.related_model)?;
            for name in relation.source_fields.iter().chain(&relation.to_fields) {
                visit.call(name)?;
            }
            visit.call(&relation.cache_name)?;
        }
        for default in &self.defaults {
            visit.call(&default.name)?;
            match &default.value {
                DefaultValue::Constant(value) | DefaultValue::Field(value) => visit.call(value)?,
            }
        }
        visit.call(&self.expression_type)?;
        visit.call(&self.assign_default)
    }

    /// Sets up `instance` from `kwargs`; false, with the instance untouched, when the arguments
    /// need `Model.__init__` - a `pk` alias, a callable value, a relation given with its key column,
    /// a value it rejects.
    fn construct(&self, instance: &Bound<'_, PyAny>, kwargs: &Bound<'_, PyDict>) -> PyResult<bool> {
        let py = instance.py();
        let column_indexes = self.column_indexes.bind(py);
        let relation_indexes = self.relation_indexes.bind(py);
        let mut assignments = Vec::with_capacity(kwargs.len() + 1);
        let mut set_by_relation = vec![false; self.defaults.len()];
        let mut custom_generated_pk = false;
        for (key, value) in kwargs.iter() {
            if let Some(index) = column_indexes.get_item(&key)? {
                let column = &self.columns[index.extract::<usize>()?];
                let Some(assigned_value) = self.get_column_value(column, value)? else {
                    return Ok(false);
                };
                if column.is_generated_primary_key {
                    custom_generated_pk = !assigned_value.is_none();
                }
                assignments.push((column.name.bind(py).clone(), assigned_value));
            } else if let Some(index) = relation_indexes.get_item(&key)? {
                let relation = &self.relations[index.extract::<usize>()?];
                if !Self::add_relation_values(relation, &value, kwargs, &mut assignments)? {
                    return Ok(false);
                }
                for &default_index in &relation.source_default_indexes {
                    set_by_relation[default_index] = true;
                }
            } else {
                return Ok(false);
            }
        }
        let false_value = PyBool::new(py, false).to_owned().into_any();
        ffi::generic_set_attribute(instance, intern!(py, "_partial"), &false_value)?;
        ffi::generic_set_attribute(instance, intern!(py, "_saved_in_db"), &false_value)?;
        ffi::generic_set_attribute(
            instance,
            intern!(py, "_custom_generated_pk"),
            PyBool::new(py, custom_generated_pk).as_any(),
        )?;
        ffi::generic_set_attribute(instance, intern!(py, "_await_when_save"), PyDict::new(py).as_any())?;
        ffi::generic_set_attribute(instance, intern!(py, "_dirty_snapshot"), py.None().bind(py))?;
        for (name, value) in &assignments {
            ffi::generic_set_attribute(instance, name, value)?;
        }
        for (default, is_set_by_relation) in self.defaults.iter().zip(set_by_relation) {
            let name = default.name.bind(py);
            if is_set_by_relation || kwargs.contains(name)? {
                continue;
            }
            match &default.value {
                DefaultValue::Constant(value) => ffi::generic_set_attribute(instance, name, value.bind(py))?,
                DefaultValue::Field(set_value) => {
                    self.assign_default.bind(py).call1((instance, name, set_value.bind(py)))?;
                }
            }
        }
        Ok(true)
    }
}
