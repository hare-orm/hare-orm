//! Ancestor tracking for Python values crossing the native parameter boundary.

use pyo3::exceptions::{PyRecursionError, PyValueError};
use pyo3::prelude::*;

#[derive(Default)]
pub(crate) struct PythonValueTraversal {
    ancestors: Vec<usize>,
}

impl PythonValueTraversal {
    // Bound recursive native frames independently of Python's interpreter limit.
    const MAX_DEPTH: usize = 256;

    pub(crate) fn visit<T>(
        &mut self,
        object: &Bound<'_, PyAny>,
        operation: impl FnOnce(&mut Self) -> PyResult<T>,
    ) -> PyResult<T> {
        let address = object.as_ptr() as usize;
        if self.ancestors.contains(&address) {
            return Err(PyValueError::new_err("circular reference in query parameter"));
        }
        if self.ancestors.len() >= Self::MAX_DEPTH {
            return Err(PyRecursionError::new_err("query parameter exceeds native nesting depth"));
        }
        self.ancestors.push(address);
        let result = operation(self);
        self.ancestors.pop();
        result
    }
}
