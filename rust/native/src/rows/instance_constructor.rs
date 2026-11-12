//! The constructor of a model's instances - the one implementation of how a constructor's keywords and
//! the defaults of the fields it got no value for are assigned.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFrozenSet, PySet, PyString, PyTuple};
use pyo3::{PyTraverseError, PyVisit};

use crate::python::ffi::generic_set_attribute;

/// How a keyword naming a field is assigned.
struct FieldEntry {
    /// The field - its `to_python`, looked up at each conversion so a replaced one counts, converts a value
    /// of none of `normalized_types`.
    field: Py<PyAny>,
    /// A forward relation (FK/O2O) - checked by Python and assigned after the plain fields.
    is_relation: bool,
    /// The types a value is assigned as it is, without `to_python()`.
    normalized_types: Py<PyFrozenSet>,
    /// Whether the field takes None.
    null: bool,
    /// The automatic primary key - set through `Model.__setattr__`, which keeps its bookkeeping.
    is_generated_pk: bool,
    /// The soft-delete field - a write of it is checked by Python.
    is_soft_delete: bool,
}

/// How a field the constructor got no value for is given its default.
struct DefaultEntry {
    name: Py<PyString>,
    field: Py<PyAny>,
    /// How the value is set - `object.__setattr__` for a plain column of a model keeping
    /// `Model.__setattr__`, else `setattr`.
    set_value: Py<PyAny>,
    /// Whether `set_value` is `object.__setattr__`.
    sets_directly: bool,
    has_db_default: bool,
    normalized_types: Py<PyFrozenSet>,
    /// The default assigned as it is - None for one worked out by Python on each construction.
    static_value: Option<Py<PyAny>>,
}

/// Assigns a new instance's keywords and defaults (`Model.__init__`) and an instance's values by field
/// name (`Model.update_from_dict()`): a value already of a type `to_python()` returns unchanged, or an
/// expression, is assigned as it is, any other is converted by the field's `to_python()`; None is
/// refused for a field that doesn't take it; every value is converted before the first is assigned.
/// Everything else - a `pk` keyword, an automatic primary key given None, a relation, the soft-delete
/// field, a callable value, an unknown keyword, a default worked out on each construction - is done by
/// the Python functions of `InstanceInitialization`, the one implementation of each.
#[pyclass(frozen, module = "rust.native.rows")]
pub struct InstanceConstructor {
    model: Py<PyAny>,
    /// Field name -> its position in `fields`.
    field_indexes: Py<PyDict>,
    fields: Vec<FieldEntry>,
    /// The primary-key fields a None keyword leaves to their generated or default value.
    automatic_keys: Py<PyTuple>,
    defaults: Vec<DefaultEntry>,
    /// Whether the model keeps `Model.__setattr__` - a new instance's plain columns are then set past it.
    sets_columns_directly: bool,
    /// `Expression` - a value of it is applied by the write, not converted.
    expression_class: Py<PyAny>,
    /// `InstanceInitialization` - the Python functions of the special cases.
    helpers: Py<PyAny>,
}

impl InstanceConstructor {
    /// Assigns the values of `kwargs` and returns what tells the fields given: `kwargs` itself, or the
    /// set of their names with the source columns of the relations given.
    fn assign_kwargs<'py>(
        &self,
        instance: &Bound<'py, PyAny>,
        kwargs: &Bound<'py, PyDict>,
        assigns_directly: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = instance.py();
        let helpers = self.helpers.bind(py);
        let mut kwargs = kwargs.clone();
        if kwargs.contains(intern!(py, "pk"))? {
            kwargs = helpers.call_method1(intern!(py, "expand_pk_kwarg"), (&self.model, &kwargs))?.cast_into()?;
        }
        let automatic_keys = self.automatic_keys.bind(py);
        for key in automatic_keys.iter() {
            if kwargs.get_item(&key)?.is_some_and(|value| value.is_none()) {
                kwargs = helpers
                    .call_method1(intern!(py, "get_kwargs_without_unset_automatic_pk"), (&kwargs, automatic_keys))?
                    .cast_into()?;
                break;
            }
        }
        let field_indexes = self.field_indexes.bind(py);
        let expression_class = self.expression_class.bind(py);
        // Every value is converted before the first is assigned - a failure leaves the instance untouched.
        let mut direct = Vec::with_capacity(kwargs.len());
        let mut relations = Vec::new();
        let mut passed: Option<Bound<'py, PySet>> = None;
        for (key, value) in kwargs.iter() {
            let Some(index) = field_indexes.get_item(&key)? else {
                helpers.call_method1(intern!(py, "raise_for_unsettable_kwarg"), (instance, &key))?;
                continue;
            };
            let entry = &self.fields[index.extract::<usize>()?];
            let field = entry.field.bind(py);
            if entry.is_relation {
                let source_fields = helpers
                    .call_method1(intern!(py, "check_relation_kwarg"), (instance, field, &key, &value, &kwargs))?;
                let passed_fields = match passed.take() {
                    Some(passed_fields) => passed_fields,
                    None => PySet::new(py, kwargs.keys())?,
                };
                for source_field in source_fields.try_iter()? {
                    passed_fields.add(source_field?)?;
                }
                passed = Some(passed_fields);
                relations.push((key.cast_into::<PyString>()?, value));
                continue;
            }
            if entry.is_soft_delete {
                helpers.call_method1(intern!(py, "check_soft_delete_kwarg"), (instance, &key))?;
            }
            let assigned = if entry.normalized_types.bind(py).contains(value.get_type())?
                || value.is_instance(expression_class)?
            {
                // Already the type to_python() returns unchanged; an expression is applied by the write.
                value
            } else if value.is_callable() {
                let (has_value, taken): (bool, Bound<'py, PyAny>) = helpers
                    .call_method1(intern!(py, "take_callable_kwarg"), (instance, &key, &value, field))?
                    .extract()?;
                if !has_value {
                    continue;
                }
                taken
            } else {
                if value.is_none() && !entry.null {
                    helpers.call_method1(intern!(py, "raise_for_none"), (&self.model, &key))?;
                }
                field.getattr(intern!(py, "to_python"))?.call1((&value,))?
            };
            direct.push((key.cast_into::<PyString>()?, assigned, assigns_directly && !entry.is_generated_pk));
        }
        for (key, value, sets_directly) in &direct {
            if *sets_directly {
                generic_set_attribute(instance, key, value)?;
            } else {
                instance.setattr(key, value)?;
            }
        }
        for (key, value) in &relations {
            instance.setattr(key, value)?;
        }
        Ok(match passed {
            Some(passed_fields) => passed_fields.into_any(),
            None => kwargs.into_any(),
        })
    }
}

#[pymethods]
impl InstanceConstructor {
    /// Args:
    ///     model: The model.
    ///     fields: Per field a keyword may name: `(name, field, is_relation, normalized_types, null,
    ///         is_generated_pk, is_soft_delete)`.
    ///     `automatic_keys`: The automatic primary-key fields.
    ///     defaults: Per field given its default: `(name, field, set_value, sets_directly,
    ///         has_db_default, normalized_types, is_static, static_value)`.
    ///     `sets_columns_directly`: Whether the model keeps `Model.__setattr__`.
    ///     `expression_class`: `Expression`.
    ///     helpers: `InstanceInitialization`.
    #[new]
    #[allow(clippy::type_complexity, clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        model: Py<PyAny>,
        fields: Vec<(Py<PyString>, Py<PyAny>, bool, Py<PyFrozenSet>, bool, bool, bool)>,
        automatic_keys: Py<PyTuple>,
        defaults: Vec<(Py<PyString>, Py<PyAny>, Py<PyAny>, bool, bool, Py<PyFrozenSet>, bool, Py<PyAny>)>,
        sets_columns_directly: bool,
        expression_class: Py<PyAny>,
        helpers: Py<PyAny>,
    ) -> PyResult<Self> {
        let field_indexes = PyDict::new(py);
        let mut field_entries = Vec::with_capacity(fields.len());
        for (index, (name, field, is_relation, normalized_types, null, is_generated_pk, is_soft_delete)) in
            fields.into_iter().enumerate()
        {
            field_indexes.set_item(&name, index)?;
            field_entries.push(FieldEntry {
                field,
                is_relation,
                normalized_types,
                null,
                is_generated_pk,
                is_soft_delete,
            });
        }
        let default_entries = defaults
            .into_iter()
            .map(
                |(name, field, set_value, sets_directly, has_db_default, normalized_types, is_static, static_value)| {
                    DefaultEntry {
                        name,
                        field,
                        set_value,
                        sets_directly,
                        has_db_default,
                        normalized_types,
                        static_value: is_static.then_some(static_value),
                    }
                },
            )
            .collect();
        Ok(InstanceConstructor {
            model,
            field_indexes: field_indexes.unbind(),
            fields: field_entries,
            automatic_keys,
            defaults: default_entries,
            sets_columns_directly,
            expression_class,
            helpers,
        })
    }

    /// Assigns a new instance's keywords, then gives every field it got no value for its default.
    fn construct(&self, instance: &Bound<'_, PyAny>, kwargs: &Bound<'_, PyDict>) -> PyResult<()> {
        let py = instance.py();
        // Not a field - set past the overridden __setattr__.
        generic_set_attribute(instance, intern!(py, "_saved_in_db"), &PyBool::new(py, false).to_owned().into_any())?;
        let passed = self.assign_kwargs(instance, kwargs, self.sets_columns_directly)?;
        let helpers = self.helpers.bind(py);
        for default in &self.defaults {
            let name = default.name.bind(py);
            if passed.contains(name)? {
                continue;
            }
            match &default.static_value {
                Some(value) if default.sets_directly => generic_set_attribute(instance, name, value.bind(py))?,
                Some(value) => instance.setattr(name, value.bind(py))?,
                None => {
                    helpers.call_method1(
                        intern!(py, "assign_field_default"),
                        (
                            instance,
                            name,
                            &default.field,
                            &default.set_value,
                            default.has_db_default,
                            &default.normalized_types,
                        ),
                    )?;
                }
            }
        }
        Ok(())
    }

    /// Assigns values by field name to an instance, each through `Model.__setattr__`.
    fn assign(&self, instance: &Bound<'_, PyAny>, kwargs: &Bound<'_, PyDict>) -> PyResult<()> {
        self.assign_kwargs(instance, kwargs, false).map(drop)
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.model)?;
        visit.call(&self.field_indexes)?;
        for entry in &self.fields {
            visit.call(&entry.field)?;
            visit.call(&entry.normalized_types)?;
        }
        visit.call(&self.automatic_keys)?;
        for default in &self.defaults {
            visit.call(&default.name)?;
            visit.call(&default.field)?;
            visit.call(&default.set_value)?;
            visit.call(&default.normalized_types)?;
            if let Some(value) = &default.static_value {
                visit.call(value)?;
            }
        }
        visit.call(&self.expression_class)?;
        visit.call(&self.helpers)
    }
}
