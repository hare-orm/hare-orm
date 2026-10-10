//! `PgParameters` - the parameter values of one statement, made by `ModelWriter` straight from the
//! attribute values: the driver binds them without reading each one from a Python object.

use pyo3::prelude::*;

use crate::pg::value::Value;

#[pyclass(frozen, sequence, module = "rust.native.pg")]
pub struct PgParameters {
    /// The values - or the error reading one of them gave, raised where a list of the values would
    /// have raised it: when a statement binds them.
    values: Result<Vec<Value>, PyErr>,
    length: usize,
}

impl PgParameters {
    pub fn new(values: Result<Vec<Value>, PyErr>, length: usize) -> Self {
        PgParameters { values, length }
    }

    /// The values, for one statement.
    pub fn get_values(&self, py: Python<'_>) -> PyResult<Vec<Value>> {
        match &self.values {
            Ok(values) => Ok(values.clone()),
            Err(error) => Err(error.clone_ref(py)),
        }
    }
}

#[pymethods]
impl PgParameters {
    fn __len__(&self) -> usize {
        self.length
    }

    /// These parameters followed by `after`, Python values - as `parameters + after` of lists.
    fn extended(&self, py: Python<'_>, after: &Bound<'_, PyAny>) -> PyResult<PgParameters> {
        let length = self.length + after.len()?;
        let mut values = self.get_values(py);
        for item in after.try_iter()? {
            let read = item?.extract::<Value>();
            if let Ok(collected) = &mut values {
                match read {
                    Ok(value) => collected.push(value),
                    Err(error) => values = Err(error),
                }
            }
        }
        Ok(PgParameters::new(values, length))
    }

    fn __repr__(&self, py: Python<'_>) -> PyResult<String> {
        let values = match &self.values {
            Ok(values) => {
                let objects = values.iter().map(|value| value.into_pyobject(py)).collect::<PyResult<Vec<_>>>()?;
                pyo3::types::PyList::new(py, objects)?.repr()?.to_string()
            }
            Err(error) => format!("<unreadable: {error}>"),
        };
        Ok(format!("PgParameters({values})"))
    }
}
