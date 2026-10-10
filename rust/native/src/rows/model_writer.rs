//! Builds the bound values of model instances - one row per instance, a value per column - as
//! Python values, or as the parameters `rust.native.pg` binds without reading each one again.

use pyo3::prelude::*;
use pyo3::types::PyList;
use pyo3::{PyTraverseError, PyVisit};

use crate::codecs::field_codec::FieldCodec;
use crate::pg::parameter_rows::PgParameterRows;
use crate::pg::parameters::PgParameters;
use crate::pg::value::Value;
use crate::python::ffi;

/// The values of a statement as they are read, keeping the first value that fails to read - a list
/// of the values fails at that value when a statement binds it.
struct ParameterCollector {
    values: Vec<Value>,
    error: Option<PyErr>,
}

impl ParameterCollector {
    fn with_capacity(capacity: usize) -> Self {
        ParameterCollector { values: Vec::with_capacity(capacity), error: None }
    }

    fn push(&mut self, read: Result<Value, PyErr>) {
        match read {
            Ok(value) if self.error.is_none() => self.values.push(value),
            Ok(_) => {}
            Err(error) => {
                if self.error.is_none() {
                    self.error = Some(error);
                }
            }
        }
    }

    /// Reads every item of `values`, Python values.
    fn push_all(&mut self, values: &Bound<'_, PyAny>) -> PyResult<()> {
        for value in values.try_iter()? {
            self.push(value?.extract::<Value>());
        }
        Ok(())
    }

    fn finish(self) -> Result<Vec<Value>, PyErr> {
        match self.error {
            Some(error) => Err(error),
            None => Ok(self.values),
        }
    }
}

/// The written columns of one model, and how each is written.
#[pyclass(frozen, module = "rust.native.rows")]
pub struct ModelWriter {
    codecs: Vec<Py<FieldCodec>>,
}

impl ModelWriter {
    fn write_instance<'py>(&self, instance: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyList>> {
        let py = instance.py();
        let mut values = ffi::ListBuilder::new(py, self.codecs.len())?;
        for (index, codec) in self.codecs.iter().enumerate() {
            let codec = codec.get();
            let value = instance.getattr(codec.name.bind(py))?;
            values.set(index, codec.write_value(value, instance)?);
        }
        Ok(values.finish())
    }

    fn write_instance_parameters(
        &self,
        instance: &Bound<'_, PyAny>,
        collector: &mut ParameterCollector,
    ) -> PyResult<()> {
        let py = instance.py();
        for codec in &self.codecs {
            let codec = codec.get();
            let value = instance.getattr(codec.name.bind(py))?;
            collector.push(codec.write_parameter(value, instance)?);
        }
        Ok(())
    }
}

#[pymethods]
impl ModelWriter {
    /// A writer of `codecs`, one per written column in column order.
    #[new]
    fn new(codecs: Vec<Py<FieldCodec>>) -> Self {
        ModelWriter { codecs }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        for codec in &self.codecs {
            visit.call(codec)?;
        }
        Ok(())
    }

    /// The row of bound values of each instance.
    fn write_rows<'py>(&self, instances: &Bound<'py, PyList>) -> PyResult<Bound<'py, PyList>> {
        let py = instances.py();
        let mut rows = ffi::ListBuilder::new(py, instances.len())?;
        for (index, instance) in instances.iter().enumerate() {
            rows.set(index, self.write_instance(&instance)?.into_any());
        }
        Ok(rows.finish())
    }

    /// The bound values of one instance.
    fn write_row<'py>(&self, instance: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyList>> {
        self.write_instance(instance)
    }

    /// The parameters of one statement: `before`, the row of each instance, then `after` - the
    /// values `before + [value for row in write_rows(instances) for value in row] + after` holds.
    fn write_parameters(
        &self,
        instances: &Bound<'_, PyList>,
        before: &Bound<'_, PyAny>,
        after: &Bound<'_, PyAny>,
    ) -> PyResult<PgParameters> {
        let before_length = before.len()?;
        let after_length = after.len()?;
        let length = before_length + instances.len() * self.codecs.len() + after_length;
        let mut collector = ParameterCollector::with_capacity(length);
        collector.push_all(before)?;
        for instance in instances.iter() {
            self.write_instance_parameters(&instance, &mut collector)?;
        }
        collector.push_all(after)?;
        Ok(PgParameters::new(collector.finish(), length))
    }

    /// The parameter rows of a statement run once per instance: each instance's row, then `after` -
    /// the rows `[[*row, *after] for row in write_rows(instances)]` holds.
    fn write_parameter_rows(
        &self,
        instances: &Bound<'_, PyList>,
        after: &Bound<'_, PyAny>,
    ) -> PyResult<PgParameterRows> {
        let width = self.codecs.len() + after.len()?;
        let mut rows = Vec::with_capacity(instances.len());
        let mut first_error = None;
        for instance in instances.iter() {
            let mut collector = ParameterCollector::with_capacity(width);
            self.write_instance_parameters(&instance, &mut collector)?;
            collector.push_all(after)?;
            match collector.finish() {
                Ok(row) if first_error.is_none() => rows.push(row),
                Ok(_) => {}
                Err(error) => {
                    if first_error.is_none() {
                        first_error = Some(error);
                    }
                }
            }
        }
        let length = instances.len();
        Ok(PgParameterRows::new(first_error.map_or(Ok(rows), Err), length))
    }

    /// The parameters of a statement binding one array per column: each column's values over the
    /// instances, in instance order - `[list(column) for column in zip(*write_rows(instances))]`.
    /// The values are written instance by instance, as by `write_rows()`.
    fn write_column_parameters(&self, instances: &Bound<'_, PyList>) -> PyResult<PgParameters> {
        let mut columns: Vec<Vec<Value>> = self.codecs.iter().map(|_| Vec::with_capacity(instances.len())).collect();
        let mut first_error = None;
        for instance in instances.iter() {
            let mut collector = ParameterCollector::with_capacity(self.codecs.len());
            self.write_instance_parameters(&instance, &mut collector)?;
            match collector.finish() {
                Ok(row) if first_error.is_none() => {
                    for (column, value) in columns.iter_mut().zip(row) {
                        column.push(value);
                    }
                }
                Ok(_) => {}
                Err(error) => {
                    if first_error.is_none() {
                        first_error = Some(error);
                    }
                }
            }
        }
        let length = columns.len();
        let values = first_error.map_or_else(|| Ok(columns.into_iter().map(Value::Array).collect()), Err);
        Ok(PgParameters::new(values, length))
    }
}
