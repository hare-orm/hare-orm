//! The rendering constraints of one parameter, without a SQL tree or query value.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::PyType;
use pyo3::{PyTraverseError, PyVisit};

#[pyclass(frozen, module = "rust.native.rows")]
pub struct ParameterSlot {
    positions: Vec<usize>,
    value_type: Py<PyType>,
    literal: bool,
    options: Py<PyAny>,
    options_getter: Option<Py<PyAny>>,
    parameter_check: Option<Py<PyAny>>,
    enum_type: Option<Py<PyType>>,
}

#[pymethods]
impl ParameterSlot {
    #[new]
    fn new(
        positions: Vec<usize>,
        value_type: Py<PyType>,
        literal: bool,
        options: Py<PyAny>,
        options_getter: Option<Py<PyAny>>,
        parameter_check: Option<Py<PyAny>>,
        enum_type: Option<Py<PyType>>,
    ) -> Self {
        Self { positions, value_type, literal, options, options_getter, parameter_check, enum_type }
    }

    #[pyo3(signature = (parameters, value, always_parameterized=false))]
    fn bind(
        &self,
        parameters: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
        always_parameterized: bool,
    ) -> PyResult<bool> {
        let py = value.py();
        if self.literal {
            return Ok(false);
        }
        if self.positions.is_empty() {
            return Ok(true);
        }
        if !value.get_type().is(self.value_type.bind(py)) {
            return Ok(false);
        }
        if let Some(getter) = &self.options_getter {
            if !getter.bind(py).call1((value,))?.eq(self.options.bind(py))? {
                return Ok(false);
            }
        }
        if !always_parameterized {
            if let Some(check) = &self.parameter_check {
                if !check.bind(py).call1((value,))?.is_truthy()? {
                    return Ok(false);
                }
            }
        }
        let mut value = value.clone();
        if let Some(enum_type) = &self.enum_type {
            while value.is_instance(enum_type.bind(py))? {
                value = value.getattr(intern!(py, "value"))?;
            }
        }
        for position in &self.positions {
            parameters.set_item(*position, &value)?;
        }
        Ok(true)
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.value_type)?;
        visit.call(&self.options)?;
        visit.call(&self.options_getter)?;
        visit.call(&self.parameter_check)?;
        visit.call(&self.enum_type)
    }
}
