//! Execute prepared references against a fresh parameter buffer for each query.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::PyTuple;
use pyo3::{PyTraverseError, PyVisit};

#[pyclass(frozen, module = "rust.native.rows")]
pub struct ParameterBindings {
    binders: Vec<Vec<Py<PyAny>>>,
    repeated: Vec<(usize, usize)>,
}

#[pymethods]
impl ParameterBindings {
    #[new]
    fn new(binders: Vec<Vec<Py<PyAny>>>, repeated: Vec<(usize, usize)>) -> Self {
        Self { binders, repeated }
    }

    /// A failed shape returns None; callbacks' conversion and validation errors propagate.
    fn bind<'py>(
        &self,
        values: &Bound<'py, PyAny>,
        template: &Bound<'py, PyAny>,
        unbindable: &Bound<'py, PyTuple>,
        model: &Bound<'py, PyAny>,
        dialect: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = values.py();
        if values.len()? != self.binders.len() {
            return Ok(py.None().into_bound(py));
        }
        for (position, first) in &self.repeated {
            if !values.get_item(*position)?.is(&values.get_item(*first)?) {
                return Ok(py.None().into_bound(py));
            }
        }
        // QueryParameters.copy preserves hidden indexes; never turn the buffer into a plain list.
        let parameters = template.call_method0(intern!(py, "copy"))?;
        for (position, binders) in self.binders.iter().enumerate() {
            let value = values.get_item(position)?;
            if value.is_none() || value.is_instance(unbindable)? {
                return Ok(py.None().into_bound(py));
            }
            for binder in binders {
                if !binder.bind(py).call1((&parameters, &value, model, dialect))?.is_truthy()? {
                    return Ok(py.None().into_bound(py));
                }
            }
        }
        Ok(parameters)
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        for binders in &self.binders {
            for binder in binders {
                visit.call(binder)?;
            }
        }
        Ok(())
    }
}
