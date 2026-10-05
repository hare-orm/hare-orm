//! `AbortOnCancel` - the `add_done_callback` of a query's asyncio future: aborts its tokio task once
//! the future is cancelled - also a task not spawned yet, which then ends at its first poll.

use futures_util::future::AbortHandle;
use pyo3::intern;
use pyo3::prelude::*;

#[pyclass(frozen)]
pub struct AbortOnCancel {
    pub task: AbortHandle,
}

#[pymethods]
impl AbortOnCancel {
    fn __call__(&self, future: &Bound<'_, PyAny>) -> PyResult<()> {
        if future.call_method0(intern!(future.py(), "cancelled"))?.is_truthy()? {
            self.task.abort();
        }
        Ok(())
    }
}
